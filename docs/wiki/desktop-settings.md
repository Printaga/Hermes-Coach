# Desktop settings page

`desktop/plugin.js` is the plugin's entire GUI: a single plain-ESM module the
Desktop shell hot-reloads. It has no build step and no dependencies beyond React
and the plugin SDK.

## Component tree

```mermaid
flowchart TD
  R[register ctx] --> S[registerSettingsPage]
  S --> M[Models: landing page]
  S --> B[Reviews]
  S --> D[Delivery]
  S --> U[Budget]
  S --> G[Guidance]
  S --> T[Status]
  M --> P[ModelPicker]
  P --> H[host.request model.options]
  G --> A[ctx.rest guidance]
  T --> Q[ctx.rest status + grant]
  B --> W[useSettings save]
  D --> W
  U --> W
  W --> X[PUT /config]
```

| Piece | Responsibility |
|---|---|
| `page(Component)` | Wraps every tab: creates one `useSettings`, reads the main model, renders a loading or error state |
| `useSettings` | Fetches `/config` and `/status`; serialises saves through a promise chain; ignores late responses after unmount |
| `ModelPicker` | Search field over a provider-grouped list; "Inherit shared model" on overrides; bounded height so it scrolls instead of clipping |
| `Card`, `Field`, `Toggle`, `Choice`, `NumberField`, `TextField` | The whole control vocabulary; each renders a label, a control, and its explanation |
| Page components | One per tab, each explaining its mode before showing controls |

## Data rules the page follows

- Build elements with `createElement`. `jsx(...)` takes children from props, so
  a variadic call drops every child and passes the first one as a key.
- Use `host`, `useValue`, and `ctx.rest(path, {method, body})`. The page context
  has no `jsx`, `useValue`, `host`, or `rest.get`; the checker fails on those.
- Save one key per PUT and let the response replace local state, so the page
  never guesses what the backend stored.
- Keep the picker's provider grouping: rows carry `slug` and `name`, models are
  strings, and a flat list loses which provider a model belongs to.

## Development loop

Because the plugin is installed as a symlink, saving this file re-syncs the
Desktop copy and hot-reloads the page. `node scripts/check-desktop-page.mjs`
runs the same checks in CI-ish form: default export shape, forbidden context
APIs, `createElement`, every setting covered, all tabs registered.

## Related pages

- [Settings API](settings-api.md) — the endpoints behind every control.
- [Build, manifest and test gates](build-and-test.md) — the page check in context.
