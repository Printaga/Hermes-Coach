# Build, manifest and test gates

The plugin ships as source: there is no compile step, no bundler, and no package
install. `plugin.yaml` declares what the host registers — tools, hooks, secret
requirements, and the minimum host version — and two scripts run the same three
quality gates on Linux/macOS (`scripts/test.sh`) and Windows
(`scripts/test.ps1`).

## What the manifest declares

| Field | Value | Consequence |
|---|---|---|
| `name` | `coach` | Plugin id, command namespace, state directory name |
| `kind` | `standalone` | Loaded from `~/.hermes/plugins/`, enabled per profile |
| `provides_tools` | `coach_ask`, `coach_decide` | The two agent-facing tools |
| `provides_hooks` | `post_llm_call`, `pre_llm_call`, `pre_verify`, `post_tool_call`, `subagent_start`, `subagent_stop`, `on_session_end` | Active review, ambient delivery, completion gate, child tracking, pruning |
| `config_schema` | **absent** | The host adds no "Agent settings" tab; the Desktop page is the only settings surface |
| `requires_env` | `OPENROUTER_API_KEY`, `TYPESAFE_API_KEY` (both optional, secret) | Prompted — skippably — at install; read at runtime through the host secret scope |
| `requires_hermes` | `>=0.22.7` | Older hosts skip the plugin instead of breaking |

`config_schema` is deliberately missing. Any schema makes Hermes render a
generic form that edits the same keys as the Coach page, and the two surfaces
then disagree. Defaults live in [configuration and state](configuration-and-state.md);
the page is the only editor.

## The three gates

```mermaid
flowchart LR
  R[ruff check .] --> E[scripts/test.sh]
  A[tests/*.py] --> B[pytest via uv]
  C[desktop/plugin.js] --> D[check-desktop-page.mjs]
  B --> E[scripts/test.sh]
  D --> E
  E --> F[exit status]
  G[plugin.yaml + package] --> H[hermes plugins doctor .]
  H --> I[install or release]
```

| Gate | Command | What it proves |
|---|---|---|
| Style and likely defects | `uvx ruff check .` (inside `test.sh`) | Import order, pyflakes errors, bugbear catches, pyupgrade modernisation |
| Python suite | `./scripts/test.sh` | Full pytest suite across settings, state, ledger, findings, advisor, jev, delivery, evidence, turn scoping, plugin hooks, command, dashboard API |
| Desktop page | `node scripts/check-desktop-page.mjs` | The page exports `{id, register}`, avoids context APIs the host does not provide, builds elements with `createElement`, covers every setting, registers all tabs, and renders each one against a route-aware stub |
| Host contract | `hermes plugins doctor .` | Manifest, import, registration: `2 tool(s), 7 hook(s)` |
| Live dispatch | `hermes -z '…'` | The real dispatcher accepts the tool's return shape |

`scripts/test.sh` runs both gates even when the first fails, and exits with the
first non-zero status, so a pytest failure cannot hide a page regression.
`COACH_PYTHON` overrides the interpreter (default `python3`), and
`SKIP_DESKTOP_CHECK=1` skips the node gate with a visible warning. Tests run
through `uv run --python "$PYTHON" --with <pinned deps>` — pytest, pyyaml,
fastapi and httpx at fixed versions — so results are reproducible and the
runtime environment stays untouched. `scripts/test.ps1` mirrors the same three
gates on Windows (ruff first, then pytest, then the page check) and folds them
into one exit status.

## Continuous integration

`.github/workflows/test.yml` runs the same scripts on every push and pull
request: `test.sh` on `ubuntu-latest` and `macos-latest`, `test.ps1` on
`windows-latest`, with Python 3.12, `uv`, and Node 22 provided by pinned
`actions/*` and `astral-sh/setup-uv` majors. The host-dependent gates (`doctor`,
live dispatch) stay manual: no Hermes install exists on the runners.

## Why the page gate exists

Three failure modes only appear in the Desktop runtime, never in pytest: the
loader rejects a default export that is not `{id, register}`; the plugin context
has no `jsx`, `useValue`, `host`, or `rest.get`; and `jsx(...)` treats its third
argument as a key, so variadic children vanish. `check-desktop-page.mjs` loads
the module with stubs and fails on all three before the app sees the file.

## Related pages

- [Plugin runtime](plugin-runtime.md) — what those hooks and tools do.
- [Settings API](settings-api.md) — the backend the page talks to.
