# Hermes Coach — agent guide

A Hermes plugin (`coach`) with a Python half, a dashboard backend, and a native
Desktop settings page. `README.md` explains what it does; this file is how to
change it without breaking the host contract.

## Commands

Run every command from the repository root.

- Everything: `./scripts/test.sh` (pytest plus the desktop page check)
- One file: `./scripts/test.sh tests/test_ledger.py`
- Page only: `node scripts/check-desktop-page.mjs`
- Host contract: `hermes plugins doctor .`
- Live dispatch: `hermes -z 'Call the coach_ask tool with {"question":"ok"} and print its exact JSON.'`
- Windows: `scripts/test.ps1`. `COACH_PYTHON` overrides the interpreter;
  `SKIP_DESKTOP_CHECK=1` skips the node gate.

There is no package install and no linter or typecheck configured. Tests run
through `uv run --python "$PYTHON" --with <pinned deps>`, so nothing is added to
the runtime environment.

## Requirements

- `uv` on PATH: every test run goes through it, and it supplies pytest,
  pyyaml, fastapi and httpx at pinned versions.
- `node` on PATH: the desktop page check.
- Any `python3` on PATH; set `COACH_PYTHON` to pin a specific interpreter
  (for example the Hermes venv).
- `fastapi` only in `dashboard/plugin_api.py`; the host provides it at runtime.

## Structure

- `plugin.yaml` — manifest and the tools/hooks the host registers. No `config_schema`.
- `__init__.py` — `register(ctx)`; imports the package lazily and fails open.
- `coach/` — pure core: `settings`, `state`, `ledger`, `findings`, `advisor`,
  `jev`, `delivery`, `evidence`, `guidance`, `worker`, `coerce`, `commands`,
  `plugin`.
  `plugin` holds lifecycle, hooks and the model surfaces; `commands` holds the
  `/coach` surface (a mixin) so the class does not carry four jobs.
- `dashboard/plugin_api.py` — `/config`, `/status`, `/guidance`,
  `/permissions/grant` for the settings page.
- `desktop/plugin.js` — the settings page: plain ESM, no build step.
- `tests/` — one file per module, plus theme files for cross-cutting
  regressions (`test_regressions_host|runtime|hooks`, `test_command_surface`,
  `test_suite_quality`). Shared fakes live in `tests/support.py`. It has no
  `__init__.py`; cross-test imports use the namespace form
  `from tests.support import plugin`.
- `docs/superpowers/` — approved design and implementation plan.

## Conventions

- Use relative imports inside the package (`from .settings import ...`). Hermes
  loads the plugin under a synthetic package name; `from coach...` fails at load
  and only `hermes plugins doctor` catches it.
- Tool handlers return `str` (JSON). Errors are `{"ok": false, "error": …}`;
  never raise out of a handler.
- Hooks are fail-open and bounded. `post_llm_call` queues work and returns; no
  hook may block or raise into a turn.
- The Review hook runs under the host's 30 s `hook_callback_timeout`. The gate
  keeps a 24 s budget (evidence up to 8 s, the review call 16 s) and skips the
  review when the window is gone: a timed-out hook is not just a lost verdict,
  the host then skips that callback and the gate goes silent. Active and
  Consult use `worker_timeout_s`.
- Settings are flat snake_case and default only in `coach/settings.py`. Do not
  add a `config_schema`: it makes the host add a duplicate "Agent settings" tab.
  `scripts/check-desktop-page.mjs` fails if a setting has no control on the page.
- The Desktop page must use `createElement` (variadic children), `host` and
  `useValue` from `@hermes/plugin-sdk`, and `ctx.rest(path, {method, body})`.
  The checker fails on `ctx.jsx`, `ctx.host`, `ctx.rest.get`, `ctx.setConfig`.
- Values from config, JSON and models go through `coach/coerce.py`: one named
  policy per rule (`as_int` rejects bool, `as_finite_float` rejects non-finite),
  so readers cannot drift apart again.
- Reasoning levels ride the app's own scale (`none`…`ultra`); a blank setting
  inherits. `Coach.ask` probes the bound host method and only then passes
  `reasoning_effort`, so a host without the kwarg keeps working.
- Store models as `{provider, model}`; never parse a `provider/model` string,
  because model ids contain slashes.
- The coach gets no tools. The plugin assembles evidence and wraps it in tags;
  strip closing tags so model text cannot close its own block.
- Completion evidence is turn-scoped: `pre_llm_call` snapshots `HEAD` once per
  turn (`_snapshot_turn_base`), and `pre_verify` collects
  `base..HEAD` + worktree + new files. Never snapshot per model call — a
  mid-turn commit would move the base past the change. Worktree-only evidence
  is the fallback, not the default. The coverage block names how many dirty
  worktree paths lie outside the turn (`_other_dirty`), so a claim that counts
  the whole tree does not read to the reviewer as work it cannot find.
- Session state: atomic writes, `0600`, hashed file names, pruned by mtime.

## Verification

1. `./scripts/test.sh`
2. `hermes plugins doctor .` — expect `2 tool(s), 7 hook(s)`.
3. Run the live probe above after changing registration, hooks, or result shapes.
   Unit tests cannot see the host dispatcher.
4. For the page: reload desktop plugins, then confirm Models lists providers
   grouped, and that saving a value survives a tab switch.

## Boundaries

- Ask first before changing the plugin's trust flags
  (`plugins.entries.coach.llm.allow_*`), the global hook timeout, or anything
  that writes the user's `config.yaml` without their click.
- Never commit `.env`, `review.json`, `~/.hermes/coach/`, or the generated
  `~/.hermes/desktop-plugins/coach/` copy. Edit `desktop/plugin.js` here. A
  symlinked package re-syncs on any byte difference and hot-reloads; a
  copied-in package only re-copies when the source mtime is newer, so reload
  desktop plugins after editing.
- No runtime dependencies: stdlib plus modules the host already ships.
- Do not weaken the same-model refusal, the budget stop, or the child-session
  refusal without updating the design doc.
