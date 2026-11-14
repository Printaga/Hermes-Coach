# Hermes Coach

A second model for the main Hermes agent. The agent can ask it for advice
mid-task, ask for a plan review before starting, or hand it a finished piece of
work; two optional modes let it review a turn automatically or gate the agent's
"done" claim.

Home: [pluginsforhermes.com](https://pluginsforhermes.com/)

Built and verified against Hermes Agent 0.23.0 on Linux.

## What it does

| Mode | Default | Trigger | What you get |
|---|---|---|---|
| Consult | on | the agent calls `coach_ask` | advice on demand; nothing is spent until it is called |
| Active | off | after each turn, per cadence | a review of the turn that just finished |
| Review | on | the agent is about to finish a turn that edited files | a bounded diff review of this turn and earlier turns in the session; it may send the agent back to work once |
| Decide | off | the agent calls `coach_decide` | a typed choice with a confidence, not prose |

Every mode uses one shared model unless you give it its own.

## How advice reaches you

- `severity` (default): blockers start a turn so the agent can fix them; notes
  and concerns become a card and reach the model at the start of its next turn.
  The review ladder has four values besides `none`: `nit` (optional), `concern`
  (a defect it can point at), `blocker` (must not ship) and **`unverifiable`** —
  the evidence block itself was incomplete. `unverifiable` is a coverage limit,
  never a claim about the work: it never holds a completion and never starts a
  turn.
- A session budget of `0` means no budget. Set a positive value to cap spending.
  Calls the host's price table cannot price (some provider routes return
  `amount_usd=None`) are costed from their token counts at
  `fallback_price_per_mtok`, so the budget still fires; set that to `0` to
  leave unpriced calls free.
- Decide API keys (`OPENROUTER_API_KEY`, `TYPESAFE_API_KEY`) are declared in
  `requires_env` and prefer the host's profile-scoped secret scope, so one
  profile's key never leaks into another's turn (pre-seam hosts fall back
  to the process environment). Set them in `~/.hermes/.env` (offered as a
  masked prompt at install); `/coach set` refuses to write a key into
  `config.yaml`. `decide_typesafe_api_key` remains as an explicit
  config-file override that no command or API writes.
- `ambient`: a card now, and the model sees the note next turn. No extra call.
- `corrective`: every note starts a turn.

## Install

Prerequisites: a Hermes build with the plugin APIs this uses (`ctx.llm`,
`ctx.inject_message`, `ctx.register_hook`, `ctx.register_tool`) and a provider
already authenticated in Hermes.

```bash
cd /path/to/Hermes-Coach
ln -sfn "$PWD" ~/.hermes/plugins/coach
hermes plugins enable coach
hermes plugins doctor .
```

`doctor` should end with `OK` and `registrations: 2 tool(s), 7 hook(s)`.

The Desktop half is opt-in. Enable it in Settings → Plugins → Coach, then use
⌘K → **Reload desktop plugins** if the page does not appear. Because the plugin
is installed as a symlink, saving `desktop/plugin.js` in this checkout
hot-reloads the page.

To remove it:

```bash
hermes plugins disable coach
rm ~/.hermes/plugins/coach
```

## First run

A fresh install is deliberately unconfigured: no model is called until you pick
one.

1. Settings → Plugins → Coach → **Models**, choose the shared coach model.
2. Choose a model that differs from your main one. The plugin refuses a coach
   running the primary model, so the second opinion is not the same blind spot.
3. If you set a per-mode model, press **Grant** on the Status tab (CLI:
   `/coach grant`) so the plugin may pass its own provider and model.
4. Optional: **Guidance** writes `COACH.md` for the current project, or a short
   global file applied everywhere.

CLI equivalent for step 1: `/coach model`.

## Agent tools

The agent gets two tools; no skill or instruction file is required, because the
tool descriptions carry their own guidance.

| Tool | Arguments | Returns |
|---|---|---|
| `coach_ask` | `question` (required, ≤4000 chars), `intent` (`advice`, `plan`, `review`), `context` (≤4000 chars) | advice text, or `{"ok": false, "error": …}` |
| `coach_decide` | `question` (required), `options` (2–6 strings), `state` (object) | `{decision, confidence, probabilities, gate, rationale}` |

`turn_id` and `session_id` are optional host hints used for the per-turn caps
and the child-session refusal; the agent does not need to send them.

## CLI

| Command | Effect |
|---|---|
| `/coach status` | model, modes, spend, pending notes |
| `/coach last [n]` | the last advice (or the last `n`) for this session |
| `/coach log [n] [full]` | recent reviews/decisions: turn, time, body, evidence, fix, question/options |
| `/coach active on\|off` | per-turn review |
| `/coach review on\|off` | completion review |
| `/coach decide on\|off` | the decide tool |
| `/coach model [mode]` | show or set a model |
| `/coach set <key> <value>` | any setting by name |
| `/coach guidance` | both guidance paths and whether they exist |
| `/coach pending`, `/coach send` | list held advice, release it now |
| `/coach pause` | pause background reviews for this session |
| `/coach grant` | write the model-override permission |
| `/coach revoke` | clear the model-override permission |
| `/coach forget` | delete this session's coach state |

Unknown input prints the full help.

## Settings

The Coach page has six tabs: **Models**, **Reviews**, **Delivery**, **Budget**,
**Guidance**, **Status**. Each control explains itself there, and the defaults
live in `coach/settings.py`.

Eight tuning constants stay in `config.yaml` under
`plugins.entries.coach.settings`: `worker_timeout_s`,
`ledger_max_tool_result_chars`, `ledger_max_recent_turns`,
`ledger_note_max_chars`, `ledger_max_tokens`, `skip_same_model`,
`allow_in_subagents`, `debug_log_decisions`.

## Decide

`coach_decide` returns `{decision, confidence, probabilities, gate, rationale}`.
`gate` is `act` at or above the confidence threshold and `escalate` below it; an
unrecognised answer also escalates.

- `jev-openrouter` (default): `typesafe/jev-1.14` through OpenRouter, billed to
  `OPENROUTER_API_KEY`.
- `jev-direct`: TypeSafe's API, needs `TYPESAFE_API_KEY` in `~/.hermes/.env`.
- `chat-model`: any chat model that returns JSON; its confidence is
  self-reported and less reliable.

## What it reads and sends

The coach is granted no host tools: it cannot read arbitrary files or run
arbitrary commands. The plugin assembles what it sees — a bounded turn
ledger, the diff of the files the agent changed for a completion review
(read-only `git` queries), and your guidance files. Calls go through
Hermes' own model routing, so credentials stay in Hermes.

The completion review covers the whole **session**, not just the current turn
and not just what is still uncommitted. The host's changed-path report resets
at every turn, so the plugin keeps its own session ledger of every path the
agent's file tools touched plus the actions that leave no diff (`memory`,
`skill_manage`, cron). The evidence is the commits this turn produced, commits
made earlier in the session, worktree changes, new files, and the earlier-turn
paths — led by a `git --stat` summary. Committing before saying "done"
therefore no longer hides the work from the reviewer. When a range is unusable
(rebase, amended history, no recorded base) the review falls back to the
worktree and says so.
The evidence block is capped by `review_evidence_max_chars` (160 000 characters
by default; adjustable on the Completion review card or with
`/coach set review_evidence_max_chars <n>`, up to 400 000). Past that the stat,
the file list and the coverage line survive, and the patch is clipped **by
name**: a bare `[patch truncated]` is gone, and the note lists every file whose
patch did not fit. Every other cap — per-file contents, path count, files
outside the working directory, the git time budget — either fits inside that
budget or states, in the coverage block, that it dropped something.

The per-turn (Active) review sees messages and one-line tool cards only, never
file contents, and its prompt says so — completion evidence belongs to the
completion review, which receives the diff.

One review is one model call. The ledger is capped at 3500 tokens and replies at
400 by default; session spend is priced from real usage against the budget you
set.

State lives in `$HERMES_HOME/coach/`: one JSON file per session (atomic,
`0600`), a JSONL review log, and the global guidance file. Sessions are pruned
to the newest twenty.

The review evidence always leads with the `git --stat`, a complete
changed-file list and a `--- coverage ---` line; only the patch is clipped when
it exceeds the budget. A file whose patch is missing has not been shown to be
unchanged, and the reviewer is told so.

Every piece of advice is also posted as a card in the conversation, so the
coach's work is visible even when the agent does not relay it. Cards can be
turned off with `delivery_show_cards`; the log and `/coach` output stay. A card
is a host notification and cannot carry buttons, so the one-click path lives on
the Desktop page: every finding under **Status → Recent reviews** has a **Send
to chat** button that submits it to this chat's composer. From the CLI, the
same action is `/coach send`.

Where the model reads it: review and Active findings become `<coach-note>`
blocks handed to the agent at the start of its next turn, and a blocked
completion is a message inside the current turn. Consult advice is the
`coach_ask` tool result (the agent is asked to relay it), `/coach last`
reprints the most recent one, and `/coach pending` lists undelivered notes
with their body. Review findings
and decisions are cards plus one line each in
`$HERMES_HOME/coach/logs/<session>.jsonl` (timestamp, kind, severity, title,
body, evidence, fix; decisions also carry the question, options and rationale;
trimmed to the newest 200). `/coach log` prints those records with the context
they carry, and `/coach log <n> full` prints fields longer than the display
cap whole. **Status → Recent reviews** shows them in the Desktop page, scoped
to **This chat** or **All sessions**.

## Troubleshooting

**The tools answer "no coach model configured".** Expected until a model is
chosen. Open Settings → Plugins → Coach.

**No Coach page in Settings.** The Desktop half is off. Enable it in
Settings → Plugins, then reload desktop plugins.

**The model picker is empty.** The host returned no providers; authenticate one
in Hermes first.

**A per-mode model fails while the shared model works.** Grant the
model-override permission on the Status tab, or run `/coach grant`.

**Active review never fires.** It is off by default, and the default cadence
ignores turns that made no tool calls.

**Completion review never fires.** It only runs on turns that edited files.

**The gate keeps sending the agent back.** Lower "Holds allowed per request";
one per request is the intended setting.

**Reviews stopped on their own.** The session budget was reached. The default
`soft` stop disables the automatic modes but leaves consults working.

## Limitations

- Tested on Linux, macOS and Windows: the CI matrix
  (`.github/workflows/test.yml`) runs the full suite on all three on every
  push.
- Messaging surfaces (Telegram, Discord, …) never receive corrective turns;
  advice stays ambient there.
- Ambient advice arrives one turn late by design, and a note whose turns expire
  is dropped rather than delivered stale.
- In very long sessions older turns collapse to one line per tool call in the
  ledger. User messages are kept; tool detail is what thins out.
- The completion gate reviews a diff, not a plan, a document, or a chat answer.
- One coach at a time: no panel and no vote.
- Listed in the Hermes plugin catalog as `coach`:
  `hermes plugins install coach`. The catalog pins the released tag, so a
  checkout is the way to run unreleased commits; `hermes plugins list` shows
  the installed version.

## Disclosures

What the plugin touches, for the catalog record:

- Network: Consult, Active and Review calls go through the host model
  router. Only the optional Decide backends call out directly —
  `https://openrouter.ai` (`jev-openrouter`) and
  `https://api.typesafe.ai` (`jev-direct`) — and only when Decide is
  enabled. No other third-party traffic, no telemetry.
- Reads: the completion review reads this turn's git diff (`base..HEAD`),
  commits made earlier in the same session (`session_base..base`), worktree
  status, new files and the paths the session change ledger recorded, through
  read-only `git` commands and direct file reads. A reported path outside the
  working directory is read as content and never diffed. The plugin never
  writes outside `$HERMES_HOME/coach/` and the project's own `COACH.md`
  guidance file.
- Processes: one bounded worker thread runs background reviews inside the
  hook budgets; the only child processes are the read-only `git` queries
  above. Nothing inherits YOLO or non-interactive mode, and anything
  awaiting a person fails rather than hangs under cron or the gateway.
- Credentials: Decide keys live in the profile environment (declared under
  `requires_env`, offered as masked prompts at install) and are read
  through the host's profile-scoped secret scope. No key is ever written
  to `config.yaml`, and no credential file is read directly.

## License

Apache 2.0 — see [LICENSE](LICENSE). Release history: [changelog.md](changelog.md).

## Demo

52-second walkthrough (three use-cases, Desktop page, all terminal output
real): [the Hackatron post on X](https://x.com/printagapub/status/2120118626111868988).

## Development

```bash
./scripts/test.sh                       # pytest + the desktop page check
./scripts/test.sh tests/test_ledger.py  # one file
node scripts/check-desktop-page.mjs     # page only
hermes plugins doctor .                 # host contract
```

`COACH_PYTHON` overrides the interpreter; `SKIP_DESKTOP_CHECK=1` skips the node
check. On Windows use `scripts/test.ps1`.

Layout: `coach/` (pure Python core; `commands.py` is the `/coach` surface), `dashboard/plugin_api.py` (settings API),
`desktop/plugin.js` (settings page), `tests/`, `scripts/`.

- Design: [docs/superpowers/specs/2026-11-05-hermes-coach-design.md](docs/superpowers/specs/2026-11-05-hermes-coach-design.md)
- Plan: [docs/superpowers/plans/2026-11-05-hermes-coach.md](docs/superpowers/plans/2026-11-05-hermes-coach.md)
- Architecture wiki: [docs/wiki/overview.md](docs/wiki/overview.md)
