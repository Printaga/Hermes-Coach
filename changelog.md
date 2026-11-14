# Changelog

## [1.0.1] - 2026-11-14

### Fixed

- The completion review now sees the **whole session**, not only the last turn.
  The host resets its changed-path list at every turn, so work finished in an
  earlier turn was reported as `unverifiable`. A per-session change ledger now
  records every `write_file`/`patch` path plus the actions that leave no diff
  (`memory`, `skill_manage`, cron), and the evidence labels which paths came
  from earlier turns.
- Committed work is diffed again. `pre_verify` carries no `turn_id`, so the
  turn-base lookup never matched and the "commits during this turn" section was
  never produced. The gate now uses the current turn's snapshot, and a
  session-wide base covers commits made in earlier turns.
- A file changed in an earlier turn, or reported outside the working directory,
  is no longer dropped from the evidence. Outside paths are read as content,
  never diffed, and the block says so.
- A failed tool call is no longer recorded as a change.
- Truncation is named everywhere: a clipped patch lists the files it did not
  show, a file over the contents budget reports how much was reviewed, and an
  exhausted git budget says so instead of reading as "nothing changed". A
  header section that has to be dropped says so too.

### Changed

- `review_evidence_max_chars` default raised from 128 000 to 160 000; the
  ceiling moves from 160 000 to 400 000. The evidence caps follow that budget
  instead of fixed constants: file list 1 000 rows, earlier-session rows 200,
  paths 1 000 (overflow counted and exemplified), outside-cwd files 100,
  session ledger 500 paths.
- The review prompt states that the evidence spans this turn and earlier turns,
  and reserves `unverifiable` for files the coverage block cannot account for.

## [1.0.0] - 2026-11-10

- Initial release.
