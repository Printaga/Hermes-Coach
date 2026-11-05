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

const body = source
  .split("\n")
  .filter((line) => !line.startsWith("import "))
  .join("\n")

const stub = `
const createElement = (type, props, ...children) => ({ type, props, children });
const useState = (initial) => [initial, () => {}];
const useEffect = () => {};
const host = { request: async () => ({ providers: [{ slug: "p", models: ["m"] }] }) };
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
const expected = ["models", "modes", "delivery", "budget", "guidance", "status"]
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

console.log(`desktop page ok: ${ids.length + 1} views`)
