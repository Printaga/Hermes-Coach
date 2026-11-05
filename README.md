# Hermes Coach

A second model for the main Hermes agent: consult it, let it review every turn,
gate completion on its verdict, or use it as a typed tiebreaker. Works in the
classic CLI and the Desktop app, with settings on the Desktop page.

## Modes

| Mode | Toggle | Default | Trigger |
|---|---|---|---|
| Consult | always on | on | the agent calls `coach_ask` |
| Active | `active_enabled` | off | `post_llm_call`, per cadence |
| Review | `review_enabled` | on | `pre_verify`, edit turns only |
| Decide | `decide_enabled` | off until a backend is set | the agent calls `coach_decide` |

Delivery of Active advice is a setting: `ambient` (card now, the model sees it
next turn, no extra call), `corrective` (a new turn now), or `severity`
(blockers corrective, everything else ambient).

## Install

```bash
ln -sfn /path/to/Hermes-Coach ~/.hermes/plugins/coach
hermes plugins enable coach
hermes plugins doctor ~/.hermes/plugins/coach
```

The Desktop half is opt-in: open Settings → Plugins → Coach and toggle it on.

## First run

A fresh install is deliberately unconfigured. Nothing calls a model until you
choose one:

1. Settings → Plugins → Coach → **Models**, pick a coach model (the same
   catalog as the main chat picker).
2. If you use per-mode models, press **Grant** on the permissions row so the
   plugin may pass its own provider/model overrides.
3. Optionally edit guidance: `COACH.md` per project, plus a short global file.

In the CLI: `/coach model`, `/coach status`, `/coach active on`, `/coach review
off`, `/coach decide on`, `/coach guidance`, `/coach pending`, `/coach send`.

## Settings

Every key is declared in `plugin.yaml` with the description the settings page
renders. Defaults live in `coach/settings.py`; a test fails if the two drift.
Advanced knobs (worker deadline, ledger caps, `skip_same_model`,
`allow_in_subagents`, `debug_log_decisions`) stay in `config.yaml` under
`plugins.entries.coach.settings`.

## State

`$HERMES_HOME/coach/` holds per-session ledgers, pending notes, spend and the
global guidance file. Project guidance is `<project>/COACH.md`. Session files
are pruned to the newest 20.

## Decide

`coach_decide` returns a typed verdict with a confidence gate. Backends:
`jev-openrouter` (default; uses `OPENROUTER_API_KEY`),
`jev-direct` (uses `TYPESAFE_API_KEY`), or `chat-model` (any chat model via a
JSON schema; weaker confidence).

## Tests

```bash
./scripts/test.sh
```

Runs pytest against the Hermes venv interpreter through `uv`, so nothing is
installed into the runtime.

## Design and plan

- `docs/superpowers/specs/2026-11-05-hermes-coach-design.md`
- `docs/superpowers/plans/2026-11-05-hermes-coach.md`
