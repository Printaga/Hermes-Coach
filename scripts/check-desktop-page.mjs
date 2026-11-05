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
 */
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { pathToFileURL } from "node:url"

const source = readFileSync(new URL("../desktop/plugin.js", import.meta.url), "utf8")

const forbidden = [
  "ctx.jsx",
  "ctx.jsxs",
  "ctx.useValue",
  "ctx.useEffect",
  "ctx.host",
  "ctx.rest.get",
  "ctx.rest.put",
  "ctx.rest.post",
  "ctx.setConfig",
]
const used = forbidden.filter((name) => source.includes(name))
if (used.length) {
  console.error(`desktop page uses APIs the plugin context does not provide: ${used.join(", ")}`)
  process.exit(1)
}
if (!source.includes("createElement")) {
  console.error("desktop page must build elements with createElement (variadic children)")
  process.exit(1)
}
if (!/export default \{[^}]*id:\s*["']coach["']/.test(source)) {
  console.error("desktop page must default-export { id, register }")
  process.exit(1)
}

// Every setting the Python side reads should have a control on the page.
// The TypeSafe key is set in .env, not on the page, so it is exempt.
const settings = readFileSync(new URL("../coach/settings.py", import.meta.url), "utf8")
const start = settings.indexOf("DEFAULTS")
const end = settings.indexOf("\n}", start)
const block = settings.slice(start, end)
const keys = [...block.matchAll(/^\s{4}"([a-z_]+)":/gm)].map((match) => match[1])
if (keys.length === 0) {
  console.error("could not read DEFAULTS from coach/settings.py")
  process.exit(1)
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
const missing = keys.filter((key) => !CONFIG_ONLY.has(key) && !source.includes(`"${key}"`))
if (missing.length) {
  console.error(`settings with no control on the page: ${missing.join(", ")}`)
  process.exit(1)
}

const body = source
  .split("\n")
  .filter((line) => !/^\s*import\s/.test(line))
  .join("\n")

const stub = `
const createElement = (type, props, ...children) => ({ type, props, children });
const useState = (initial) => [initial, () => {}];
const useRef = (initial) => ({ current: initial });
const useEffect = () => {};
const useValue = () => "";
const host = { state: { model: "" }, request: async () => ({ providers: [{ slug: "p", models: ["m"] }] }) };
`

const dir = mkdtempSync(join(tmpdir(), "coach-page-"))
const file = join(dir, "plugin.mjs")
writeFileSync(file, stub + body)

const mod = await import(pathToFileURL(file).href)
const plugin = mod.default
if (!plugin || plugin.id !== "coach" || typeof plugin.register !== "function") {
  console.error("desktop page default export is not { id: 'coach', register }")
  process.exit(1)
}

let page = null
plugin.register({
  registerSettingsPage: (value) => {
    page = value
    return () => {}
  },
  rest: async () => ({ values: {}, permissions: {} }),
})

if (!page || page.id !== "coach" || !Array.isArray(page.children)) {
  console.error("register() did not contribute a coach settings page")
  process.exit(1)
}
const ids = page.children.map((child) => child.id)
const expected = ["behaviour", "delivery", "budget", "guidance", "status"]
for (const id of expected) {
  if (!ids.includes(id)) {
    console.error(`settings page is missing the ${id} tab`)
    process.exit(1)
  }
}

for (const child of [{ id: "overview", render: page.render }, ...page.children]) {
  const tree = child.render({})
  if (!tree || !tree.type) {
    console.error(`settings tab ${child.id} rendered nothing`)
    process.exit(1)
  }
}

console.log(`desktop page ok: ${ids.length + 1} views, ${keys.length} settings covered`)
