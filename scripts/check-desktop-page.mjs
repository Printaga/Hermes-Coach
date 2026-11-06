#!/usr/bin/env node
/**
 * Static + smoke check for desktop/plugin.js.
 *
 * The Python suite cannot see the Desktop half, and both bugs that made the
 * settings page fail to mount were invisible to `node --check`:
 *   - it used context APIs the host does not provide (ctx.jsx, ctx.useValue,
 *     ctx.host, ctx.rest.get, ctx.setConfig), and
 *   - it default-exported the bare register function instead of {id, register}.
 * A third, the variadic `h(...)` calls, only works with createElement.
 *
 * This script fails on any of those before the page reaches the app.
 *
 * On precision: the static checks strip comments and single/double-quoted
 * strings before matching, and use token-aware patterns (member access with
 * optional chaining, bracket access, destructuring) instead of raw
 * `source.includes(name)`. They still cannot see through aliases
 * (`const x = ctx; x.host`) — the runtime smoke test below is the backstop
 * for those, and review should treat any new `ctx` plumbing with suspicion.
 */
import { execFileSync } from "node:child_process"
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { pathToFileURL } from "node:url"

let dir = null
function fail(message) {
  console.error(message)
  if (dir) {
    try {
      rmSync(dir, { recursive: true, force: true })
    } catch {
      // The gate message is what matters; cleanup is best effort.
    }
  }
  process.exit(1)
}

try {
  // main() imports the generated module: awaiting it keeps cleanup after the
  // pending import, and routes async rejections through fail().
  await main()
} catch (err) {
  fail(`desktop page check failed: ${err && err.message ? err.message : err}`)
} finally {
  if (dir) {
    try {
      rmSync(dir, { recursive: true, force: true })
    } catch {
      // Best effort: never leave /tmp/coach-page-* behind.
    }
  }
}

function main() {
  const source = readFileSync(
    new URL("../desktop/plugin.js", import.meta.url),
    "utf8",
  )

  // Strip block comments, line comments, and single/double-quoted strings so
  // a mention in prose or an error message cannot trip (or satisfy) a check.
  // Template literals are kept: code inside ${} stays analyzable.
  // Strings first: a literal may contain // or /*, and stripping comments
  // first corrupted the following code. Template literals are removed too —
  // their text is display data, never a gate target.
  const code = source
    .replace(/'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"/g, '""')
    .replace(/`(?:[^`\\]|\\.)*`/g, '""')
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:\\])\/\/.*$/gm, "$1")

  // Context APIs the host does not provide, in every access spelling.
  // Member access is matched on `code` (comments/strings removed); bracket
  // access necessarily quotes its key, so it is matched on a copy that keeps
  // strings but drops comments.
  const withStrings = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:\\])\/\/.*$/gm, "$1")
  const ctxName = String.raw`\bctx\s*(?:\?\.\s*|\.\s*)`
  const bracket = (name) =>
    String.raw`\bctx\s*(?:\?\.\s*)?\[\s*['"]${name}['"]\s*\]`
  const APIs = [
    ["jsx", String.raw`\bctx\s*\.\s*jsx\b`, null],
    ["jsxs", String.raw`\bctx\s*\.\s*jsxs\b`, null],
    ["useValue", `${ctxName}useValue\\b`, bracket("useValue")],
    ["useEffect", `${ctxName}useEffect\\b`, bracket("useEffect")],
    ["host", `${ctxName}host\\b`, bracket("host")],
    [
      "rest.get",
      `${ctxName}rest\\s*(?:\\?\\.\\s*|\\.\\s*)get\\b`,
      `${bracket("rest")}\\s*(?:\\?\\.\\s*|\\.\\s*)get\\b`,
    ],
    [
      "rest.put",
      `${ctxName}rest\\s*(?:\\?\\.\\s*|\\.\\s*)put\\b`,
      `${bracket("rest")}\\s*(?:\\?\\.\\s*|\\.\\s*)put\\b`,
    ],
    [
      "rest.post",
      `${ctxName}rest\\s*(?:\\?\\.\\s*|\\.\\s*)post\\b`,
      `${bracket("rest")}\\s*(?:\\?\\.\\s*|\\.\\s*)post\\b`,
    ],
    ["setConfig", `${ctxName}setConfig\\b`, bracket("setConfig")],
  ]
  const used = []
  for (const [label, memberPattern, bracketPattern] of APIs) {
    if (new RegExp(memberPattern).test(code)) used.push(`ctx.${label}`)
    else if (bracketPattern && new RegExp(bracketPattern).test(withStrings))
      used.push(`ctx[${label}]`)
  }
  for (const [pattern, label] of [
    [/\{\s*[^}]*\bhost\b[^}]*\}\s*=\s*ctx\b/, "destructured ctx.host"],
    [/\{\s*[^}]*\b(rest|setConfig)\b[^}]*\}\s*=\s*ctx\b/, "destructured ctx.rest"],
  ]) {
    if (pattern.test(code) || pattern.test(withStrings)) used.push(label)
  }
  if (used.length) {
    fail(
      `desktop page uses APIs the plugin context does not provide: ${used.join(", ")}`,
    )
  }
  if (!/\bcreateElement\b/.test(code)) {
    fail("desktop page must build elements with createElement (variadic children)")
  }
  // The default-export shape is asserted behaviorally below
  // (mod.default.id === 'coach', typeof register === 'function'); a regex
  // cannot cover `export { x as default }` or multiline objects, so none is used.

  // Every setting the Python side reads should have a control on the page.
  // The TypeSafe key is set in .env, not on the page, so it is exempt.
  const settings = readFileSync(
    new URL("../coach/settings.py", import.meta.url),
    "utf8",
  )
  let keys = []
  try {
    const out = execFileSync(
      process.env.COACH_PYTHON || "python3",
      [
        "-c",
        "import json,sys;sys.path.insert(0,sys.argv[1]);import coach.settings as s;print(json.dumps(list(s.DEFAULTS)))",
        new URL("..", import.meta.url).pathname,
      ],
      { encoding: "utf8" },
    )
    keys = JSON.parse(out)
  } catch (err) {
    fail(`could not import coach.settings for the DEFAULTS list: ${err.message}`)
  }
  if (keys.length === 0) {
    fail("could not read DEFAULTS keys from coach/settings.py")
  }
  // Tuning constants that stay in config.yaml on purpose (docs in README).
  const CONFIG_ONLY = new Set([
    "decide_typesafe_api_key",
    "worker_timeout_s",
    "ledger_max_tool_result_chars",
    "ledger_max_recent_turns",
    "ledger_note_max_chars",
    "ledger_max_tokens",
    "skip_same_model",
    "allow_in_subagents",
    "debug_log_decisions",
  ])
  const missing = keys.filter((key) => {
    if (CONFIG_ONLY.has(key)) return false
    const saveCall = new RegExp(`save\\(\\s*["']${key}["']`)
    const valuesProp = new RegExp(`values\\.${key}\\b`)
    if (saveCall.test(code) || valuesProp.test(code)) return false
    // Per-mode models render through a mapped ModelPicker: the key appears as
    // a string literal in the overrides list and saves via save(key, ...).
    if (
      ["coach_model", "active_model", "review_model", "consult_model"].includes(key) &&
      source.includes(`"${key}"`) &&
      /save\(\s*key\b/.test(code)
    ) {
      return false
    }
    return true
  })
  if (missing.length) {
    fail(`settings with no control on the page: ${missing.join(", ")}`)
  }

  // Integrations the page must keep, matched quote- and spacing-tolerantly so
  // a formatting-only edit cannot fail the gate.
  // Structural snippets are matched against the stripped `code` (a comment
  // cannot satisfy them); literal UI copy is matched against `source`, because
  // stripping strings would erase the very text the page must render.
  const required = [
    [/host\s*\.\s*request\s*\(\s*["']model\.options["']/, 'host.request("model.options")', "source"],
    [/["']Search models or providers["']/, '"Search models or providers"', "source"],
    [/["']This chat["']/, '"This chat"', "source"],
    [/["']All sessions["']/, '"All sessions"', "source"],
    [/method\s*:\s*["']PUT["']/, 'method: "PUT"', "source"],
    [/["']\/permissions\/grant["']/, '"/permissions/grant"', "source"],
    [/["']\/permissions\/revoke["']/, '"/permissions/revoke"', "source"],
    [/["']Refresh["']/, '"Refresh"', "source"],
    [/role["']?\s*:\s*["']status["']/, 'role: "status"', "source"],
    [/\bproviderName\b/, "providerName", "code"],
    [/host\s*\.\s*composer\b/, "host.composer", "code"],
    [/composer\s*\.\s*submit\s*\(/, "composer.submit()", "code"],
  ]
  const missingSnippets = required
    .filter(([pattern, , target]) =>
      target === "source" ? !pattern.test(source) : !pattern.test(code),
    )
    .map(([, label]) => label)
  if (missingSnippets.length) {
    fail(`desktop page is missing required integrations: ${missingSnippets.join(", ")}`)
  }

  // Strip imports depth-aware: single-line and multi-line `import ... from`
  // alike are removed, so the module body below never executes host imports
  // or side effects on import. `export ... from` re-exports would do the
  // same and are stripped too. Dynamic import() in the body is not
  // transformed — none exists, and the smoke test would surface one.
  const kept = []
  let skipping = false
  let braces = 0
  for (const line of source.split("\n")) {
    if (!skipping && /^\s*import\b/.test(line)) {
      skipping = true
      braces = 0
    }
    if (!skipping && /^\s*export\b[^;]*\bfrom\b/.test(line)) {
      continue
    }
    if (!skipping && /^\s*export\s*\{[^}]*$/.test(line)) {
      // A multiline re-export (`export { a,\n b } from "x"`) would execute on
      // import just like an import; skip it to its closing brace and `from`.
      skipping = true
      braces = (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length
      continue
    }
    if (skipping) {
      // Braces inside strings/comments must not close the statement early.
      const bare = line
        .replace(/'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"/g, '""')
        .replace(/`(?:[^`\\]|\\.)*`/g, '""')
        .replace(/\/\/.*$/, "")
      braces += (bare.match(/\{/g) || []).length - (bare.match(/\}/g) || []).length
      const done = (/\bfrom\b\s*["']/.test(bare) || /;\s*$/.test(bare)) && braces <= 0
      if (done) skipping = false
      continue
    }
    kept.push(line)
  }
  if (skipping) {
    fail("could not strip imports from desktop/plugin.js (unterminated import?)")
  }
  const body = kept.join("\n")

  const stub = `
const createElement = (type, props, ...children) => {
  if (typeof type === "function") {
    return type({ ...props, children });
  }
  return { type, props, children };
};
let __hookIndex = 0;
let __hooks = [];
const useState = (initial) => {
  const i = __hookIndex++;
  if (!(i in __hooks)) __hooks[i] = (typeof initial === "function" ? initial() : initial);
  const set = (v) => { __hooks[i] = (typeof v === "function" ? v(__hooks[i]) : v); };
  return [__hooks[i], set];
};
const useRef = (initial) => ({ current: initial });
const useEffect = (fn) => {
  try {
    const out = fn();
    if (out && typeof out.then === "function") {
      out.catch((err) => { globalThis.__effectError = err; });
    }
  } catch (err) {
    globalThis.__effectError = err;
  }
};
const useValue = () => "";
const host = { state: { model: "", cwd: "", focusedSessionId: "" }, request: async () => ({ providers: [{ slug: "p", name: "P", models: ["m"] }] }), composer: { submit: () => true } };
const __resetHooks = () => { __hookIndex = 0; };
const __resetAll = () => { __hookIndex = 0; __hooks = []; globalThis.__effectError = null; };
globalThis.__resetHooks = __resetHooks;
globalThis.__resetAll = __resetAll;
`

  dir = mkdtempSync(join(tmpdir(), "coach-page-"))
  const file = join(dir, "plugin.mjs")
  writeFileSync(file, stub + body)

  return import(pathToFileURL(file).href).then((mod) => {
    if (
      typeof mod.flattenModels !== "function" ||
      typeof mod.groupModels !== "function"
    ) {
      fail("desktop page must export flattenModels and groupModels for testing")
    }
    const fixture = mod.flattenModels({
      providers: [
        { slug: "openrouter", name: "OpenRouter", models: ["a/b", { model: "c" }, ""] },
        { slug: "", name: "", models: ["solo"] },
        null,
      ],
    })
    if (
      fixture.length !== 3 ||
      fixture[0].provider !== "openrouter" ||
      fixture[0].providerName !== "OpenRouter" ||
      fixture[0].model !== "a/b" ||
      fixture[2].providerName !== "Other"
    ) {
      fail("desktop model catalog mapping lost provider rows")
    }
    const openGroups = mod.groupModels(fixture, "OPEN")
    if (openGroups.length !== 1 || openGroups[0].items.length !== 2) {
      fail("desktop model search must be case-insensitive across providers and models")
    }
    if (mod.groupModels(fixture, "no-such-model").length !== 0) {
      fail("desktop model search must return no groups without matches")
    }
    if (typeof mod.findingPrompt !== "function") {
      fail("desktop page must export findingPrompt for testing")
    }
    const prompt = mod.findingPrompt({
      severity: "concern",
      title: "Retry hides the timeout",
      body: "The loop swallows the deadline error.",
      evidence: "client.py:88",
      fix: "Re-raise after the last attempt.",
    })
    for (const expected of [
      "Coach concern: Retry hides the timeout",
      "swallows the deadline error",
      "Evidence: client.py:88",
      "Suggested fix: Re-raise",
      "Address this finding",
    ]) {
      if (!prompt.includes(expected)) {
        fail(`send-to-chat prompt lost "${expected}"`)
      }
    }
    if (mod.findingPrompt({ severity: "none", title: "t" }).startsWith("Coach none")) {
      fail("send-to-chat prompt must not label a sourceless note as a severity")
    }
    const plugin = mod.default
    if (!plugin || plugin.id !== "coach" || typeof plugin.register !== "function") {
      fail("desktop page default export is not { id: 'coach', register }")
    }

    let page = null
    const called = new Set()
    return Promise.resolve(
      plugin.register({
        registerSettingsPage: (value) => {
          page = value
          return () => {}
        },
        // Route-aware stub: every endpoint the page calls returns a realistic
        // shape, so render paths that depend on the payload are exercised.
        // Unknown routes throw: a new endpoint without a fixture fails loudly.
        rest: async (url = "", opts = {}) => {
          const method = (opts && opts.method) || "GET"
          called.add(`${method} ${String(url).split("?")[0]}`)
          if (url.startsWith("/config")) {
            if (method === "PUT") {
              return {
                values: (opts.body && opts.body.values) || {},
                permissions: { granted: false, provider_overrides: false, model_overrides: false },
              }
            }
            return {
              values: { active_enabled: true, delivery_show_cards: true },
              permissions: { granted: false, provider_overrides: false, model_overrides: false },
            }
          }
          if (url.startsWith("/status")) {
            return {
              configured: false,
              coach_model: null,
              active_enabled: true,
              review_enabled: true,
              decide_enabled: false,
              decide_backend: "jev-openrouter",
              spend_usd: 0,
              spend_total_usd: 0,
              spend_sessions: {},
              budget_usd: 5,
              budget_per_session_usd: 5,
              pending_notes: 0,
              sessions: 0,
              permissions: { granted: false, provider_overrides: false, model_overrides: false },
            }
          }
          if (url.startsWith("/recent")) {
            return {
              reviews: [
                {
                  session: "s",
                  at: "2026-01-01",
                  severity: "nit",
                  title: "t",
                  body: "b",
                },
              ],
            }
          }
          if (url.startsWith("/guidance")) {
            if (method === "PUT") return { message: "Saved.", path: "/tmp/g" }
            return { text: "", path: "", scope: "global", cwd: "" }
          }
          if (url.startsWith("/permissions/grant")) return { ok: true }
          if (url.startsWith("/permissions/revoke")) return { ok: true }
          throw new Error(`unexpected REST call in smoke test: ${method} ${url}`)
        },
      }),
    ).then(async () => {
      if (!page || page.id !== "coach" || !Array.isArray(page.children)) {
        fail("register() did not contribute a coach settings page")
      }
      for (const child of page.children) {
        if (!child || typeof child.id !== "string" || typeof child.render !== "function") {
          fail("settings page has a malformed child entry (needs id + render)")
        }
      }
      const ids = page.children.map((child) => child.id)
      const expected = ["behaviour", "delivery", "budget", "guidance", "status"]
      for (const id of expected) {
        if (!ids.includes(id)) {
          fail(`settings page is missing the ${id} tab`)
        }
      }

      const tabs = [{ id: "overview", render: page.render }, ...page.children]
      if (typeof page.render !== "function") {
        fail("settings page has no overview render")
      }
      for (const child of tabs) {
        let tree
        try {
          globalThis.__resetAll()
          tree = child.render({})
          // Let queued effects resolve, then re-render with loaded state.
          await new Promise((resolve) => setTimeout(resolve, 0))
          await new Promise((resolve) => setTimeout(resolve, 0))
          globalThis.__resetHooks()
          tree = child.render({})
          if (globalThis.__effectError) {
            const err = globalThis.__effectError
            fail(
              `settings tab ${child.id} threw inside an effect: ${err && err.message ? err.message : err}`,
            )
          }
        } catch (err) {
          if (String(err && err.message).startsWith("desktop page check failed")) {
            throw err
          }
          fail(`settings tab ${child.id} threw while rendering: ${err.message}`)
        }
        if (!tree || !tree.type) {
          fail(`settings tab ${child.id} rendered nothing`)
        }
        // Deep walk: the tab must render substance, not an empty shell, and
        // no branch of the tree may be malformed.
        if (substance(tree) < 3) {
          fail(`settings tab ${child.id} rendered almost nothing`)
        }
      }
      for (const route of ["GET /status", "GET /recent"]) {
        if (!called.has(route)) {
          fail(`desktop page never called ${route}`)
        }
      }

      console.log(
        `desktop page ok: ${ids.length + 1} views, ${keys.length} settings covered`,
      )
    })
  })
}

// Count rendered content: element nodes plus non-empty text, recursing
// through arrays and the children a stub createElement collects.
function substance(node) {
  if (node == null || typeof node === "boolean") return 0
  if (Array.isArray(node)) {
    return node.reduce((total, child) => total + substance(child), 0)
  }
  if (typeof node !== "object") {
    return typeof node === "string" && node ? 1 : 0
  }
  let total = node.type ? 1 : 0
  const kids =
    node.children !== undefined
      ? node.children
      : node.props
        ? node.props.children
        : undefined
  return total + substance(kids)
}
