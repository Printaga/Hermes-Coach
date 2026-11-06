# Hermes Coach — plugin design

Status: draft for approval
Date: 2026-11-05
Target: Hermes Agent v0.22.7 (b200f5bc60e3), CLI + Desktop

## 1. Intent

The main session agent gets a second model it can consult: for a second
opinion before starting, for help mid-task, and for a review of what it just
did. The same package also offers two optional automatic behaviours — a
per-turn reviewer, and a gate that fires when the agent is about to claim it
is done — plus a typed tiebreaker for genuine forks.

It must work identically in the classic CLI and the Desktop app, be
configurable without editing YAML, and cost little enough that leaving it on
is not a decision.

## 2. Capabilities

| Mode | Toggle | Default | Trigger | Cost profile |
|---|---|---|---|---|
| Consult | none — always on | on | agent calls `coach_ask` | only when called |
| Active | `active_enabled` | **off** | `post_llm_call`, per cadence | one review per reviewed turn |
| Review | `review_enabled` | on | `pre_verify`, edit turns only | one review per qualifying turn |
| Decide | `decide_enabled` | off until a backend is set | agent calls `coach_decide` | input tokens only |

`Consult` is the product. The other three are optional amplifiers.

## 3. Goals

- One package, both surfaces, one source of truth for settings and state.
- Every mode's spend is measured and bounded.
- The coach has no agency: no tools, no shell, no writes, no long-term memory
  beyond the ledger and guidance files.
- Settings are understandable from their labels alone.
- Works with providers the user already has; Jev is optional.
- A coach failure never blocks, delays past its deadline, or corrupts a turn.

## 4. Non-goals (v1)

- Watching `delegate_task` children or other sessions.
- More than one coach (no panel, no vote).
- A coach that reads files or runs commands.
- Streaming advice, or advice rendered inside the assistant's own reply.
- Jev gating every tool call.
- A web dashboard tab. The dashboard backend exists only to serve the
  Desktop settings page.
- Per-mode reasoning effort: `ctx.llm.complete` exposes no reasoning knob.

## 5. Anti-goals

- Advice that interrupts a turn the user is watching.
- Advice that repeats itself across turns.
- The coach reviewing its own output.
- Cost that grows with transcript length.
- A settings page that needs documentation.

## 6. Constraints and verified seams

Verified against the installed tree, not against docs or other plugins.

| Seam | Status | Used for |
|---|---|---|
| `ctx.register_hook("post_llm_call")` | verified; payload has `session_id`, `turn_id`, `user_message`, `assistant_response`, `conversation_history`, `model`, `platform`, `side_question_token` | Active |
| `ctx.register_hook("pre_verify")` | verified; payload has `session_id`, `platform`, `model`, `coding`, `attempt`, `final_response`, `changed_paths`; fires when a text answer would end a turn that edited files | Review |
| `plugins.hook_callback_timeout` | verified; bounded hooks get 30 s by default, are abandoned fail-open on timeout, suppressed for 60 s after one, and skipped after 3 abandoned workers | Review's call budget must stay under it |
| `ctx.register_hook("pre_llm_call")` | verified; `{"context": "..."}` is appended to the current user message as an `api_content` sidecar — model sees it, transcript does not, cache prefix stays stable | Ambient delivery |
| `ctx.inject_message(text)` | verified; keyless call from a hook routes to the live CLI or the TUI/Desktop session queue, and starts a turn | Corrective delivery |
| `ctx.post_message(text)` | verified; renders a card/notification on Desktop and TUI, a line at turn end on classic CLI; invisible to the model | Visible advice cards |
| `ctx.register_tool(name, toolset, schema, handler)` | verified; handlers must return `str` | `coach_ask`, `coach_decide` |
| `ctx.register_command(name, handler)` | verified | `/coach` |
| `ctx.llm.complete` / `complete_structured` | verified; provider/model overrides gated by `plugins.entries.coach.llm.allow_*_override` | coach calls |
| `PluginLlmUsage` | verified; input, output, cache-read, cache-write tokens | cost meter |
| `agent.usage_pricing.estimate_usage_cost` | verified | dollars, not guesses |
| `ctx.ask_choice(question, choices)` | verified; clarify prompt on CLI, TUI, Desktop, messaging | CLI model picker |
| `ctx.get_config` / `ctx.set_config` | verified | settings reads/writes from Python |
| Desktop `model.options` RPC | verified | model picker data |
| Desktop `ctx.registerSettingsPage` | verified | settings page |
| `dashboard/manifest.json` + `dashboard/plugin_api.py` (`APIRouter`, mounted at `/api/plugins/<id>/`) | verified | guidance editor, status |
| `ctx.request_model_selection` | **absent in this build** — do not use | — |
| `ctx.register_status_segment` | **absent in this build** — do not use | — |

Two consequences: the model picker is built from `model.options` (Desktop) and
`ask_choice` (CLI), and there is no status-bar chip; status lives in
`/coach status` and on the settings page.

## 7. Approaches considered

### 7.1 Delivery of Active advice

| Option | Cost | Latency | Chosen |
|---|---|---|---|
| Ambient: card + `pre_llm_call` context | review only, +100–300 tokens | one turn late | default for nits/concerns |
| Corrective: `inject_message` | a full extra turn, often with tools | immediate | blockers only, capped |
| Severity policy (both, by severity) | mixed | mixed | **default setting** |

Corrective-by-default was rejected: it roughly doubles Active mode's cost,
which is the one thing the design exists to avoid.

### 7.2 What the coach sees

| Option | Verdict |
|---|---|
| Coach with read/grep tools | rejected: every investigative call replays its context, and every file read is an injection surface |
| Only what the agent types into the tool | rejected for Active/Review: the agent becomes the sole witness to its own work |
| **Plugin as evidence collector** | chosen: deterministic, capped, identical every run |

The coach never has tools. The plugin assembles evidence: a bounded ledger for
Active, a bounded diff of the turn for Review (commits made since the turn
started, the worktree, and new files), and the agent's own question plus ledger
tail for Consult. Each reviewer is told what it can see: Active its ledger,
Review its diff, and that a missing diff is a plugin limit rather than a
finding about the work.

### 7.3 Guidance scope

| Option | Verdict |
|---|---|
| Global only | rejected: accumulates rules from every project and is paid on every call in every project |
| Project only | acceptable fallback |
| **Both, project primary** | chosen: one editor, two-way scope switch |

Project guidance lives in `<cwd>/COACH.md` so it can be committed and shared.
Global guidance is a short defaults file, a dozen lines by intent.

### 7.4 Decide backend

`jev-openrouter` (default; uses the existing OpenRouter key, endpoint
`POST https://openrouter.ai/api/alpha/decisions`), `jev-direct`
(`https://api.typesafe.ai/v1/systemone`, needs `TYPESAFE_API_KEY`), or
`chat-model` (any chat model via `complete_structured`; weaker calibration,
works everywhere). Jev is not a chat model and never appears in the coach
model picker.

### 7.5 Mode packaging

Four independent toggles, not one enum. They compose: Active only, Review
only, both, or neither, with Decide available in all four cases.

## 8. Architecture

```
hermes-coach/
  plugin.yaml                 # manifest, config_schema, provides_hooks, provides_tools
  __init__.py                 # register(ctx): hooks, tools, command, state load
  coach/
    settings.py               # schema defaults, resolve model per mode, same-model refusal
    state.py                  # per-profile index + per-session state, atomic writes, pruning
    ledger.py                 # turn digest compiler + token budget
    evidence.py               # bounded git diff for Review; path confinement
    prompts.py                # system prompts per intent; guidance assembly
    findings.py               # structured output parse, normalise, dedupe hash
    advisor.py                # one LLM call, deadline, usage + cost accounting
    jev.py                    # three decide backends
    delivery.py               # ambient / corrective / card routing per surface
    worker.py                 # single-flight background queue
    command.py                # /coach ...
  dashboard/
    manifest.json             # {name, label, icon, api: "plugin_api.py"}
    plugin_api.py             # GET status, GET/PUT guidance, GET config
  desktop/
    plugin.js                 # registerSettingsPage: models, modes, delivery,
                              # budget, guidance editor, status
  tests/
```

### 8.1 Data flow — Consult

1. Agent calls `coach_ask({intent, question, context?})`.
2. Handler loads the session ledger tail, builds `messages` (system + guidance
   + ledger tail + question), calls `ctx.llm.complete` with the Consult model.
3. Returns plain text advice as the tool result. One call, no loop.

### 8.2 Data flow — Active

1. `post_tool_call` increments a per-turn tool counter.
2. `post_llm_call` snapshots the completed turn, checks cadence and skip rules,
   and enqueues a review. Hook returns immediately.
3. Worker: single-flight. If a review is running, keep only the newest queued
   turn; drop older ones. Checks the budget before calling. Deadline 45 s.
4. On completion: verify the response came from the requested provider and
   model — a failed call can fall back to the primary model. A mismatch is
   recorded as `fallback_mismatch` and discarded. Then normalise the finding;
   if `none`, record and stop, otherwise store it and deliver per policy.
5. Ambient notes are held per session and returned by the next `pre_llm_call`
   as `{"context": ...}`, once, then marked delivered. A card is posted at
   review time.

### 8.3 Data flow — Review gate

1. `pre_verify` fires with `changed_paths` and `final_response`.
2. Skip if a hold was already issued for this request.
3. Evidence, composed under `ctx.session_cwd()` and capped at 8k characters,
   covers the **whole turn**, not only what is still uncommitted:
   - `pre_llm_call` recorded the commit `HEAD` pointed at when the turn started
     (once per turn — a turn spans several model calls, and re-snapshotting
     after a mid-turn commit would move the base past the change).
   - `git diff <base>..HEAD -- <paths>` for commits the turn produced,
     `git diff HEAD -- <paths>` for tracked files still uncommitted, and the
     head of each untracked path marked `(new file)`.
   - A `git --stat` line leads the block, so an over-budget diff keeps the file
     list and clips the patch instead of losing one or the other.
   - Unusable range (rebase, amended history, detached or missing base) falls
     back to the worktree diff and the block says `not an ancestor of HEAD`.
     Outside a repo: file heads.
   - Skip only when the composed evidence is empty: no commits and no worktree
     change means there is nothing to review.
4. One LLM call with the Review model, 20 s deadline — the hook itself is
   bounded at 30 s by the host, and a timed-out callback is suppressed for 60 s
   afterwards. Prompt: "does the change satisfy the request; is there a defect
   the claim hides?" A missing diff is a coverage limit of the plugin, never a
   finding about the work (at most a nit).
5. `blocker` and `authority=hold_once` and budget allows → return
   `{"action": "continue", "message": "<required fix>"}`. Everything else →
   post a card, return `None`. A timeout is a card or nothing, never a hold.

### 8.4 Data flow — Decide

`coach_decide({question, options[], state?})` → backend call → compact JSON:
`{decision, confidence, probabilities, gate, rationale, cost_usd}`. Below
`decide_min_confidence`, `gate` is `escalate` regardless of the argmax.

### 8.5 State layout

```
$HERMES_HOME/coach/
  state.json                 # per-profile: defaults, last used session
  guidance.md                # global COACH.md content
  sessions/<session_id>.json # ledger, session note, pending notes, holds,
                             # counters, spend, last error
  logs/<session_id>.jsonl    # one line per review: severity, tokens, cost
```

Settings live in `config.yaml` under `plugins.entries.coach.settings` so
`ctx.get_config`, the generic Desktop form and our page all read one place.
Session state is pruned on `on_session_end` and capped at 20 sessions.

### 8.6 Threading and recursion

One daemon worker thread per process, one in-flight call, coalescing queue.
Deadline 45 s; the hook never waits. The worker exits with the process.

Coach calls go through `agent/auxiliary_client.call_llm`, which fires
`pre_auxiliary_call`/`post_auxiliary_call`, not `pre_llm_call`/`post_llm_call`,
so a review cannot trigger a review. A thread-local `_in_coach_call` guard is
still set as belt-and-braces.

### 8.7 First run

Shipping state is `coach_model` empty. That is an explicit unconfigured state,
not a silent default:

- `coach_ask` / `coach_decide` return one clear line: no coach model is set,
  with the two ways to set it.
- Review stays quiet: no calls, no cards, no holds. `/coach status` and the
  settings page both say "Unconfigured — choose a coach model".
- The page opens on a model picker with a single call to action, and shows the
  permissions row if the override flags are not granted yet.
- Nothing is auto-picked and nothing inherits the primary model. The first
  coach call happens only after the user chooses.

## 9. Settings contract

Keys are flat snake_case: `config_schema` keys with dots are path segments and
would nest. Values are validated by the existing loader.

`type` is the manifest type; `picker` and `text` are rendered by our page as
richer controls. Every `description` below is the literal text rendered under
the label.

### Models

| Key | Type | Default | Description |
|---|---|---|---|
| `coach_model` | json `{provider, model}` | empty — unconfigured | Model every mode uses unless it has its own. Until one is chosen the plugin is unconfigured: tools say so and Review stays quiet. Same catalog as the main chat picker. |
| `active_model` | json, empty = inherit | inherit | Override for Active. This mode runs every turn, so a cheap fast model here saves the most. |
| `review_model` | json, empty = inherit | inherit | Override for Review. It gates completion, so this is where the strongest model earns its cost. |
| `consult_model` | json, empty = inherit | inherit | Override for Consult. |
| `max_reply_tokens` | int | 400 | Hard cap on the coach's answer. Output is the expensive half of a review; this is the main lever. |

### Permissions

Not a form field: one status row with an action, because these two keys live
under `plugins.entries.coach.llm`, not under `settings`.

| Row | Default | Description |
|---|---|---|
| Model overrides | not granted | Per-mode models need `allow_provider_override` and `allow_model_override`. **Grant** writes both flags for the profile this page is scoped to, then re-reads them. Until granted, any non-inherit model fails at call time; inheriting needs no permission. |

### Modes

| Key | Type | Default | Description |
|---|---|---|---|
| `active_enabled` | bool | false | Reviews each completed turn and delivers advice by the delivery policy. Off by default because it is the only mode that spends on every turn. |
| `active_cadence` | enum `tool_turns_only` `every_turn` `every_n` | `tool_turns_only` | Which turns Active reviews. `tool_turns_only` skips turns where the agent only talked; `every_n` reviews one turn in N. |
| `active_every_n` | int | 3 | Turns per review when the cadence is `every_n`. |
| `review_enabled` | bool | true | Reviews the diff when the agent is about to claim it is done. Fires only on turns that edited files. |
| `review_authority` | enum `hold_once` `report` | `hold_once` | `hold_once` may send the agent back to work one time per request; `report` only attaches findings. |
| `review_max_holds` | int | 1 | Holds allowed per user request. Above one, the gate turns into an argument. |
| `decide_enabled` | bool | false | Exposes `coach_decide` for typed fork resolution. Needs a backend below. |
| `decide_backend` | enum `jev-openrouter` `jev-direct` `chat-model` | `jev-openrouter` | Where decisions come from. `jev-openrouter` uses your OpenRouter key; `jev-direct` uses a TypeSafe key; `chat-model` works with any chat model but has weaker confidence. |
| `decide_model` | str | `typesafe/jev-1.14` | The decision model. Not a chat model — it returns verdicts, not prose. |
| `decide_min_confidence` | float | 0.60 | Below this the tool answers `escalate` instead of picking. Raise it for destructive choices. |
| `decide_max_calls_per_turn` | int | 3 | Stops an agent from outsourcing its judgement one call at a time. |
| `consult_max_calls_per_turn` | int | 4 | Same cap for consultations. |
| `decide_typesafe_api_key` | secret | — | Only for `jev-direct`. Stored in `.env` as `TYPESAFE_API_KEY`, never in `config.yaml`. |

### Delivery

| Key | Type | Default | Description |
|---|---|---|---|
| `delivery_policy` | enum `severity` `ambient` `corrective` | `severity` | `ambient` = card now, model sees it next turn, no extra call. `corrective` = new turn now, agent acts. `severity` = blockers corrective, everything else ambient. |
| `delivery_max_corrective_per_request` | int | 1 | Per user request. Stops advice from turning into a conversation of its own. |
| `delivery_show_cards` | bool | true | Post a card for ambient notes. Off means advice reaches the model but you never see it. |
| `delivery_note_ttl_turns` | int | 2 | Undelivered notes are dropped after this many turns instead of surfacing stale. |

### Budget

| Key | Type | Default | Description |
|---|---|---|---|
| `budget_session_usd` | float | 0.50 | Coach spend per session, computed from real usage, not estimates of calls. |
| `budget_on_exceed` | enum `warn` `soft` `hard` | `soft` | `warn` keeps going; `soft` stops Active, Review and Decide but leaves explicit consults; `hard` stops everything. |

### Guidance

| Key | Type | Default | Description |
|---|---|---|---|
| `guidance_enabled` | bool | true | Send guidance to the coach. Off ignores both files without deleting them. |

The editor is not a config field. It writes `$HERMES_HOME/coach/guidance.md`
(global) and `<focused project>/COACH.md` through `dashboard/plugin_api.py`,
with a scope switch, a character and token count, and an 8k cap. Files are the
only home; there are no `guidance_*` content keys in `config.yaml`, so the
generic form cannot disagree with the editor.

### Advanced (config.yaml only, not on the page)

| Key | Default | Meaning |
|---|---|---|
| `worker_timeout_s` | 45 | Deadline for one coach call. |
| `ledger_max_tool_result_chars` | 400 | Per-result truncation in the ledger. |
| `ledger_max_recent_turns` | 3 | Turns kept as tool cards. |
| `ledger_note_max_chars` | 1000 | Size of the coach's running intent note. |
| `ledger_max_tokens` | 3500 | Ceiling for the assembled ledger. |
| `skip_same_model` | true | Refuse a coach model identical to the primary. |
| `debug_log_decisions` | false | Log prompt sizes, parse results and delivery routing. |

## 10. Token and cost model

The ledger is the primary saving. Replaying a transcript makes cost grow with
session length; a compiled ledger makes it flat.

- Every user message is carried verbatim — intent must not be summarised away.
- Tool calls become one line: name, main argument, status, size.
- Tool results truncate to 400 chars; file bodies and reasoning never enter.
- The last 3 turns keep tool cards; anything older collapses to counts.
- The coach keeps a running note of the objective (≤1000 chars), rewritten by
  the coach, labelled as its own interpretation.
- Assembly stops at 3500 estimated tokens; oldest tool cards drop first, user
  messages last.
- System prompt and guidance are byte-stable across calls, so providers with
  automatic prefix caching (DeepSeek, OpenAI, OpenRouter passthrough) hit the
  cache on every call after the first.
- One finding per review, ≤400 output tokens. `none` is a valid, common answer.
- Skip rules, evaluated before any call: turn had no tool calls and cadence is
  `tool_turns_only`; the same tool-call hash was reviewed within 2 turns; the
  primary model equals the coach model and `skip_same_model`; budget exhausted;
  a review is already in flight for a newer turn.
- Passive and Decide cost nothing until used.

Cost per call is computed from `PluginLlmUsage` via
`agent.usage_pricing.estimate_usage_cost(model, usage, provider=...)`, so the
meter reflects real provider pricing, including cache reads. Jev responses
carry their own `usage.cost`; the chat-model backend is priced the same way as
any other call. Session spend is the sum, persisted with the session state.

## 11. Failure behaviour and safety

- Every coach failure is swallowed and recorded: deadline, provider error,
  parse failure, missing key. The turn always continues.
- Reviews are fail-open. The Review gate is fail-open except when it has a
  parsed blocker, so a broken coach never blocks completion.
- The coach never executes anything. The plugin runs read-only `git`
  (`status`, `diff`, `rev-parse`, `merge-base`) itself under `ctx.session_cwd()`,
  with a timeout, capped output, path confinement, and no external diff or
  textconv drivers from repo config.
- Evidence is wrapped in a fenced block and the system prompt states that
  evidence is data, never instructions. With no tools, a successful injection
  can only produce bad advice.
- Guidance writes: global to `$HERMES_HOME/coach/guidance.md`; project to
  `<focused cwd>/COACH.md` only when that directory was recorded from a real
  session, never a symlink target, never a new directory, capped at 8k chars.
- A coach response whose provider/model differs from the requested pair is
  discarded as `fallback_mismatch`, so a silent fallback can never make the
  coach review the primary model's own work.
- `coach_ask` and `coach_decide` refuse in child sessions, tracked through the
  `subagent_start` / `subagent_stop` hooks. `allow_in_subagents` (config only)
  turns that off.
- Secrets are read from the environment only for the backend that needs them.
  The OpenRouter key is never logged or persisted.
- Advice is always attributed (`Coach`, with severity) so the model and the
  user can tell it from their own words.
- Instructed stance: "weigh this, do not obey it" — advice is not a policy
  override.

## 12. Surface parity checklist

Every capability must be reachable and visible on both surfaces:

| Capability | CLI | Desktop |
|---|---|---|
| Set model per mode | `/coach model [mode]` via `ask_choice` | picker on the settings page |
| Toggle modes | `/coach active on\|off`, `/coach review on\|off`, `/coach decide on\|off` | switches |
| Delivery, budget | `/coach set <key> <value>` | form controls |
| Guidance | `/coach guidance` (paths + contents) | editor, both scopes |
| Status and spend | `/coach status` | status tab |
| Pending advice | `/coach pending`, `/coach send` | listed in the status tab, with deliver/dismiss |
| Model-override permission | `/coach grant` | Grant button |
| Unconfigured state | `/coach status` and the tools say so | blocking prompt on the page |
| Reviews and decisions | tools | same tools, same state |

Anything created on one surface must be listed on the other: a pending note or
a hold is stored in session state, not in the page's memory.

## 13. Decisions made in conversation

- Four modes, four toggles, Active off by default.
- Delivery is a setting, not a fixed behaviour; default `severity`.
- Coach has no tools; the plugin collects evidence.
- Project guidance is primary, global is a short defaults file, one editor with
  a scope switch.
- Per-mode model overrides with inherit, provider and model stored separately.
- Jev via OpenRouter is the default decision backend; it never enters the
  coach-model picker.
- Budget default is a soft stop (reviews stop, explicit consults still work).
- The plugin may write its own model-override trust flags, but only behind an
  explicit Grant click on its settings page.
- A fresh install starts unconfigured: no auto-picked model, no inherited
  primary, no background reviews until the user chooses.
- Guidance lives in files only, edited through the plugin's own backend.

## 14. Left to the builder

- Exact prompt wording and the JSON schema field names for findings.
- Ledger line format and the estimator used for the 3500-token ceiling.
- Whether the worker uses a thread or an asyncio task per surface.
- Desktop picker layout and search behaviour.
- The OpenRouter decisions request envelope: confirm `model` plus
  `{state, questions}` against the API reference before coding; the direct
  TypeSafe shape is documented in the askjev skill.
- Test seams for hooks: how to emulate `post_llm_call`, `pre_verify` and
  `pre_llm_call` payloads without a live turn.
- The exact config writer and profile scope for the Grant button
  (`load_config` → `save_config`; the page passes the profile it is scoped to).
- The field name carrying the child session id in the `subagent_start` payload,
  used for the child-session refusal.
- Packaging and catalog entry, if this ships beyond this machine.

## 15. Risks

- A fresh install is inert until a model is chosen; the page and `/coach status`
  must say so plainly or it reads as broken.
- Desktop plugin halves load disabled by default; a first-run "nothing
  appears" report is the toggle, not the code.
- `inject_message` is gateway-routed on messaging platforms and needs
  `allow_gateway_injection`; on those surfaces, degrade to ambient.
- Structured output is not honoured by every provider; parse failure must land
  on `none`, never on a fabricated finding.
- Held advice can outlive its relevance; TTL and the dedupe hash are the
  guards, not good intentions.
