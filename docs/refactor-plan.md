# Refactor follow-ups from the code audit

Status after the follow-up pass on `f6fccf1` (uncommitted): steps 1-6, 8 and 9
are implemented; step 7 is partly done. Everything below is covered by
`./scripts/test.sh` (ruff, 434 tests, desktop page check) and
`hermes plugins doctor .` (2 tools, 7 hooks).

## Done

1. **`coach/logs.py`** (finding 34) — path, append envelope, trim and the two
   read shapes (`read_tail`, `last_row`) in one owner. `plugin.py` and
   `commands.py` no longer build the path or the lock/chmod/trim envelope; the
   upward import to `commands` for `_log_name`/`_now_iso` is gone.
2. **`TurnCounters`** (finding 14) — `coach/counters.py` owns the
   `namespace:turn` key, reserve/release/prune. All four caps and
   `_prune_calls` use it; global telemetry is exempt by construction.
3. **Shared `_reserve`** (finding 33) — one load/budget/cap/reserve/save
   critical section for `_ask` and `_decide`; the per-mode budget policy is an
   argument.
4. **Priced admission, partly** (finding 22) — `_call_floor()` holds
   `fallback_price_per_mtok × max_reply_tokens` per in-flight call against the
   budget check and releases it at charge. It narrows the overshoot to the
   input-token side; a true reservation needs a token estimate of the prompt.
5. **Cross-process store lock** (finding 19) — `coach/lockfile.py` `StoreLock`
   combines the re-entrant thread lock with an advisory `flock`/`msvcrt` lock on
   `<store>/.lock`. Two-process exclusion is tested.
6. **`collect` split** (finding 38) — `_collect_discovered`, `_git_unavailable`,
   `_empty_change_set` and `_evidence_text` extracted; `collect` went from 165
   to 101 lines with no behaviour change.
8. **Post-effect saves** (finding 20) — `_save_with_retry` retries a charge save
   once and logs what could not persist; `deliver` catches a failed note save
   and releases the corrective slot.
9. **Page draft hook** (finding 35) — `useDraft`, `draftInput` and
   `controlIdFor` shared by `TextField` and `NumberField`; `TextField` now has
   a real `id`/`htmlFor` pair.

## Remaining

7b. **Move the tool schemas out of `plugin.py`** (finding 32). `ASK_SCHEMA`,
   `DECIDE_SCHEMA` and their descriptions are pure literals (about 125 lines)
   and can move to `coach/schemas.py`, with `plugin.py` re-exporting them so
   `__init__.py` keeps working. After that the gate method and the worker
   plumbing are the only large bodies left; do not split the stateful hook
   bodies. The "under 1 000 lines" target is aspirational, not a requirement.

7c. **A true budget reservation** (finding 22 remainder). Estimate the prompt
   cost before the call (`ledger.estimate_tokens(messages) × fallback price`)
   and reserve that plus the reply floor; reconcile against the priced reply.
   The current floor already stops a burst of calls from passing the same
   pre-call spend figure.
