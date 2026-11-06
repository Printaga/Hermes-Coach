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
  main()
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
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:\\])\/\/.*$/gm, "$1")
    .replace(/'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"/g, '""')

  // Context APIs the host does not provide, in every access spelling.
  const ctxName = String.raw`\bctx\s*(?:\?\.\s*|\.\s*)`
  const bracket = (name) => String.raw`\bctx\s*\[\s*['"]${name}['"]\s*\]`
  const forbidden = [
    [/ctx\s*\.\s*jsx\b/.source, "ctx.jsx"],
    [String.raw`\bctx\s*\.\s*jsxs\b`, "ctx.jsxs"],
    [new RegExp(`${ctxName}useValue\\b`).source, "ctx.useValue"],
    [new RegExp(`${ctxName}useEffect\\b`).source, "ctx.useEffect"],
    [new RegExp(`${ctxName}host\\b|${bracket("host")}`).source, "ctx.host"],
    [new RegExp(`${ctxName}rest\\s*(?:\\?\\.\\s*|\\.\\s*)get\\b`).source, "ctx.rest.get"],
    [new RegExp(`${ctxName}rest\\s*(?:\\?\\.\\s*|\\.\\s*)put\\b`).source, "ctx.rest.put"],
    [new RegExp(`${ctxName}rest\\s*(?:\\?\\.\\s*|\\.\\s*)post\\b`).source, "ctx.rest.post"],
    [new RegExp(`${ctxName}setConfig\\b`).source, "ctx.setConfig"],
    [/\{\s*[^}]*\bhost\b[^}]*\}\s*=\s*ctx\b/.source, "destructured ctx.host"],
  ]
  const used = forbidden
    .filter(([pattern]) => new RegExp(pattern).test(code))
    .map(([, label]) => label)
  if (used.length) {
    fail(
      `desktop page uses APIs the plugin context does not provide: ${used.join(", ")}`,
    )
  }
  if (!code.includes("createElement")) {
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
  const defaultsAt = settings.search(/DEFAULTS\b[^=]*=\s*\{/)
  if (defaultsAt === -1) {
    fail("could not locate DEFAULTS assignment in coach/settings.py")
  }
  let depth = 0
  let end = -1
  for (let i = settings.indexOf("{", defaultsAt); i < settings.length; i++) {
    if (settings[i] === "{") depth++
    else if (settings[i] === "}") {
      depth--
      if (depth === 0) {
        end = i
        break
      }
    }
  }
  if (end === -1) {
    fail("could not find the end of DEFAULTS in coach/settings.py")
  }
  const block = settings.slice(defaultsAt, end)
  const keys = [...block.matchAll(/"([a-z_]+)"\s*:/g)].map((match) => match[1])
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
  const missing = keys.filter(
    (key) => !CONFIG_ONLY.has(key) && !source.includes(`"${key}"`),
  )
  if (missing.length) {
    fail(`settings with no control on the page: ${missing.join(", ")}`)
  }

  // Integrations the page must keep, matched quote- and spacing-tolerantly so
  // a formatting-only edit cannot fail the gate.
  const required = [
    [/host\s*\.\s*request\s*\(\s*["']model\.options["']/, 'host.request("model.options")'],
    [/"Search models or providers"/, '"Search models or providers"'],
    [/"This chat"/, '"This chat"'],
    [/"All sessions"/, '"All sessions"'],
    [/method\s*:\s*["']PUT["']/, 'method: "PUT"'],
    [/["']\/permissions\/grant["']/, '"/permissions/grant"'],
    [/\bproviderName\b/, "providerName"],
  ]
  const missingSnippets = required
    .filter(([pattern]) => !pattern.test(source))
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
    if (skipping) {
      braces += (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length
      const done =
        (/\bfrom\b\s*["']/.test(line) || /;\s*$/.test(line)) && braces <= 0
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
const createElement = (type, props, ...children) => ({ type, props, children });
const useState = (initial) => [initial, () => {}];
const useRef = (initial) => ({ current: initial });
const useEffect = () => {};
const useValue = () => "";
const host = { state: { model: "" }, request: async () => ({ providers: [{ slug: "p", models: ["m"] }] }) };
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
    const plugin = mod.default
    if (!plugin || plugin.id !== "coach" || typeof plugin.register !== "function") {
      fail("desktop page default export is not { id: 'coach', register }")
    }

    let page = null
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
          if (url.startsWith("/config")) {
            if (method === "PUT") {
              return {
                values: (opts.body && opts.body.values) || {},
                permissions: {},
              }
            }
            return {
              values: { active_enabled: true, delivery_show_cards: true },
              permissions: { model_overrides: false },
            }
          }
          if (url.startsWith("/status")) {
            return {
              configured: false,
              spend_usd: 0,
              budget_usd: 5,
              pending_notes: 0,
              sessions: 0,
              permissions: { model_overrides: false },
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
          throw new Error(`unexpected REST call in smoke test: ${method} ${url}`)
        },
      }),
    ).then(() => {
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
          tree = child.render({})
        } catch (err) {
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
