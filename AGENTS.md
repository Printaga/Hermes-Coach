# Hermes Coach — agent guide

A Hermes plugin (`coach`) with a Python half, a dashboard backend, and a native
Desktop settings page. `README.md` explains what it does; this file is how to
change it without breaking the host contract.

## Commands

Run every command from the repository root.

- Everything: `./scripts/test.sh` (ruff, pytest, and the desktop page check)
- One file: `./scripts/test.sh tests/test_ledger.py`
- Page only: `node scripts/check-desktop-page.mjs`
- Host contract: `hermes plugins doctor .`
- Live dispatch: `hermes -z 'Call the coach_ask tool with {"question":"ok"} and print its exact JSON.'`
- Windows: `scripts/test.ps1`. `COACH_PYTHON` overrides the interpreter;
  `SKIP_DESKTOP_CHECK=1` skips the node gate.
- CI: `.github/workflows/test.yml` runs `test.sh` (`test.ps1` on Windows)
  on ubuntu/macos/windows for every push and PR.

There is no package install. `ruff` is the only linter (`ruff.toml`, run as a
gate by `scripts/test.sh`); there is no typecheck configured. Tests run
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

- `plugin.yaml` — manifest: tools/hooks the host registers, `requires_env`
  secret declarations, `requires_hermes` floor. No `config_schema`.
- `__init__.py` — `register(ctx)`; imports the package lazily and fails open.
- `coach/` — pure core: `settings`, `state`, `ledger`, `findings`, `advisor`,
  `jev`, `delivery`, `evidence`, `guidance`, `worker`, `coerce`, `scrub`,
  `cards`, `counters`, `lockfile`, `logs`, `prompts`, `commands`, `trustflags`,
  `secrets`, `plugin`.
  `plugin` holds lifecycle, hooks and the model surfaces; `commands` holds the
  `/coach` surface (a mixin) so the class does not carry four jobs;
  `trustflags` is the only writer of the `llm.allow_*` flags.
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
- Credentials come only through the host: new secrets are declared in
  `plugin.yaml` under `requires_env` and read with
  `coach/secrets.py:get_secret` (the `agent.secret_scope` profile-scoped
  route). Never read `os.environ`, `.env` files, or `auth.json` directly
  for a credential, and never write one to `config.yaml` — `SECRET_KEYS`
  in `coach/settings.py` is the refusal list every writer checks.
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
- Completion evidence is SESSION-scoped, and the host payload is not: the host
  sends `pre_verify` only `changed_paths` for the CURRENT turn (its
  `_turn_file_mutation_paths` resets every turn) and no `turn_id`. So
  `post_tool_call` records every `write_file`/`patch` path into a per-session
  ledger (`coach/changes.py`; a failed call changed nothing and is not
  recorded), `pre_llm_call` snapshots `HEAD` once per turn
  (`_snapshot_turn_base`) AND once per session (`session_base`), and
  `pre_verify` collects the turn's `base..HEAD` + the session's
  `session_base..base` + worktree + new files + the earlier-turn paths. Never
  snapshot per model call — a mid-turn commit would move the base past the
  change. The base lookup must fall back to the latest snapshot when the
  payload has no `turn_id`, or committed work is never diffed. Reported
  absolute paths outside cwd are read as content (never diffed), because
  dropping them silently made a claimed skill/notes edit look missing. The
  coverage block labels which paths came from earlier turns and names how many
  dirty worktree paths lie outside the set (`_other_dirty`), so neither a
  multi-turn claim nor a whole-tree count reads as work the reviewer cannot
  find.
- Session state: atomic writes, `0600`, hashed file names, pruned by mtime.
  The change ledger (`changes`, `actions`, `session_base`, `turn_seq`,
  `turn_id`) rides the same file, flushed once per model call, never per tool
  call.

## Release

- Bump `version` in `plugin.yaml` **and** `dashboard/manifest.json` together:
  the host reads the first, the desktop page the second, and nothing else keeps
  them equal. `tests/test_manifest.py` asserts the pair matches.
- Add a `changelog.md` entry under the new version, using `Added` / `Changed` /
  `Fixed` headings. The changelog is the reader-facing record; the README links
  to it and does not restate it.
- The catalog pins a released tag, so a catalog install stays on the tagged
  version until the tag and entry move; a checkout always runs the working
  tree.

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
