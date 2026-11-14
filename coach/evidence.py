"""Bounded diff evidence for the Review gate.

``collect`` asks git for the change set under the supplied paths and falls
back to reading the files directly when git is unavailable or fails. Paths
under the working directory are project-relative: a ``..`` component is
refused and an absolute path is accepted only when it resolves inside. An
absolute path the host reported OUTSIDE the working directory is read as
content only (never passed to git, never diffed), because dropping it silently
made a claimed skill or notes edit look like work the reviewer never received.

Evidence covers a whole session, not one turn: commits made since the turn
started (``base``), commits made in earlier turns of the same session
(``session_base``), and whatever is still uncommitted. The host's
``changed_paths`` is reset at every turn, so ``prior`` supplies the paths
earlier turns changed and the block labels them as such. A git ``--stat``
line leads the block so a size cap degrades to a summary instead of hiding
which files changed.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["collect", "head"]

_GIT_TIMEOUT = 2
_GIT_TOTAL_BUDGET = 8.0
_MAX_LISTED_FILES = 1000
_MAX_PRIOR_ROWS = 200
_PRIOR_PATCH_CHARS = 200_000
_MAX_OUTSIDE = 100
# The reader's own cap: the largest single file any caller may ask for. The
# per-file share of the contents budget is applied by _heads.
_FILE_HEAD_CHARS = 400_000
# Fallback total for callers that pass no budget. The plugin always passes the
# configured evidence budget, so this only bounds direct/test calls. A file or
# section that does not fit is named, never silently dropped.
_HEADS_TOTAL_CHARS = 160_000
_MAX_PATHS = 1000
_HARDENED = [
    "git",
    "-c",
    "core.quotePath=false",
    # Repo config must not run code. fsmonitor is a command git executes on
    # status and diff (probe-confirmed: a repo's core.fsmonitor ran during the
    # gate), and the diff flags below cover the pager, external diffs and
    # textconv drivers.
    "-c",
    "core.fsmonitor=false",
    "--no-pager",
    # A reported path is a path, not a pattern. Without this a changed file
    # named "*.py" (or a leading ":") is expanded by git and the diff no longer
    # matches the host's change set.
    "--literal-pathspecs",
]


_FILTER_CACHE: dict[tuple[str, float], list[str]] = {}
_FILTER_KEY = r"^filter\..*\.(clean|smudge|process)$"


def _filter_overrides(
    root: Path, runner: Callable[..., object], timeout: float
) -> list[str]:
    """``-c filter.<name>.<kind>=`` for every driver the repo config defines.

    A clean/smudge/process command runs whenever git hashes a worktree file, so
    a repository's own config executes code during a diff (probe-confirmed).
    An empty value disables the driver; enumerating config is enough because an
    attribute naming a driver with no config entry runs nothing.
    """
    try:
        stamp = (root / ".git" / "config").stat().st_mtime
    except OSError:
        stamp = 0.0
    key = (str(root), stamp)
    cached = _FILTER_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_FILTER_CACHE) > 512:
        # One entry per (repo, config-mtime): a churn-heavy run would creep
        # without a cap; wholesale clear is fine — entries are cheap to redo.
        _FILTER_CACHE.clear()
    overrides: list[str] = []
    result = _git(
        runner,
        root,
        ["git", "-c", "core.quotePath=false", "config", "--get-regexp", _FILTER_KEY],
        timeout=timeout,
    )
    if result is not None and result.returncode == 0:
        for line in _stdout(result).splitlines():
            name = line.split(None, 1)[0].strip()
            if name.count(".") >= 2:
                overrides.extend(["-c", f"{name}="])
    overrides = overrides[:120]
    # Cache only a probe that actually answered: exit 0 is "drivers found",
    # exit 1 is "none declared". A timeout or a hard failure returns [] for
    # this call but must not be cached as if the repo declared no drivers.
    if result is not None and result.returncode in (0, 1):
        _FILTER_CACHE[key] = overrides
    return overrides


def _hardened(
    root: Path, runner: Callable[..., object], timeout: float
) -> list[str]:
    """The hardened git prefix, including this repo's filter overrides."""
    return [*_HARDENED, *_filter_overrides(root, runner, timeout)]


def _read_text(path: Path, limit: int = _FILE_HEAD_CHARS) -> str | None:
    """Read at most ``limit`` characters from a regular file.

    The gate runs inside the host's hook callback, so a database dump or a
    FIFO in the change set must not be decoded whole or block the read.
    """
    try:
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


def _safe_paths(root: Path, paths: Sequence[str]) -> list[str]:
    """Every reported path that resolves inside ``root``, de-duplicated.

    No length cap here: the caller slices, so the overflow count it reports is
    the real number of valid paths it dropped, not "inputs minus accepted"
    (which counted duplicates and invalid paths as overflow).
    """
    try:
        base = root.resolve()
    except (OSError, ValueError, RuntimeError):
        return []
    safe: list[str] = []
    seen: set[str] = set()
    for raw in paths or []:
        text = str(raw or "").strip()
        if not text:
            continue
        candidate = Path(text)
        if candidate.is_absolute():
            # The host records absolute paths; accept one only when it resolves
            # inside the project, and pass it to git as a relative path.
            try:
                value = candidate.resolve().relative_to(base).as_posix()
            except (OSError, ValueError, RuntimeError):
                continue
            if value in seen:
                continue
            seen.add(value)
            safe.append(value)
            continue
        if ".." in candidate.parts:
            continue
        try:
            (base / candidate).resolve().relative_to(base)
        except (OSError, ValueError, RuntimeError):
            continue
        value = candidate.as_posix()
        if value in seen:
            continue
        seen.add(value)
        safe.append(value)
    return safe


def _toplevel(directory: Path, runner: Callable[..., object], timeout: float) -> str:
    """The git toplevel containing ``directory``, or ``""`` outside any repo."""
    result = _git(runner, directory, [*_HARDENED, "rev-parse", "--show-toplevel"], timeout=timeout)
    if result is None or result.returncode != 0:
        return ""
    return _stdout(result).strip()


def _discover_repo(
    root: Path,
    safe: Sequence[str],
    runner: Callable[..., object],
    remaining: Callable[[], float],
) -> tuple[Path, list[str], list[str]]:
    """Find the git repo under which most reported paths actually live.

    The plugin's cwd is not always the worktree the change set belongs to:
    agents whose cwd sits above their repo (``/home/user`` for a project in
    ``~/Development/...``) produced reviews whose evidence said "git was
    unavailable" because ``git status`` ran in the home directory. Discovery
    groups the same reported paths by their enclosing api toplevel, owned by
    the group with the most paths; containment still guards every read, so
    the trust boundary is unchanged.
    """
    counts: dict[str, int] = {}
    ordered: list[str] = []
    memo: dict[str, str] = {}
    for rel in safe:
        try:
            target = (root / rel).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        direction = target.parent if target.is_file() else target
        key = str(direction)
        if key in memo:
            top = memo[key]
        else:
            top = _toplevel(direction, runner, min(_GIT_TIMEOUT, remaining()))
            memo[key] = top
        if not top:
            continue
        try:
            target.relative_to(Path(top))
        except (OSError, ValueError, RuntimeError):
            continue
        if top not in counts:
            counts[top] = 0
            ordered.append(top)
        counts[top] += 1
    if not counts:
        return root, [], []
    ranked = sorted(ordered, key=lambda top: (-counts[top], top))
    best = ranked[0]
    others: list[str] = []
    rels: list[str] = []
    seen: set[str] = set()
    for top in ranked:
        for rel in safe:
            try:
                target = (root / rel).resolve()
                inner = target.relative_to(Path(top)).as_posix()
            except (OSError, ValueError, RuntimeError):
                continue
            if inner in seen or not inner:
                continue
            if top == best:
                seen.add(inner)
                rels.append(inner)
            else:
                others.append(f"{inner} (in {top})")
    return Path(best), rels, others


_DISCOVERY_NOTE = (
    "--- repo discovery ---\n"
    "The plugin's cwd was not inside a git repo, so collection ran in the\n"
    "repo found from the report's paths: {root}\n{extra}"
)


def _git(
    run: Callable[..., object],
    root: Path,
    argv: list[str],
    timeout: float = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess | None:
    if timeout <= 0:
        # The shared budget is spent: do not floor the timeout and overrun it.
        return None
    try:
        result = run(
            argv,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result if hasattr(result, "returncode") else None


def _stdout(result: object) -> str:
    value = getattr(result, "stdout", "")
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def head(cwd: str, *, run: Callable[..., object] | None = None) -> str:
    """Return the commit HEAD points at, or ``""`` outside a usable repo."""
    root = Path(cwd)
    result = _git(run or subprocess.run, root, ["git", "rev-parse", "HEAD"])
    if result is None or result.returncode != 0:
        return ""
    return _stdout(result).strip()


def _diff(
    runner: Callable[..., object],
    root: Path,
    rev: str,
    paths: Sequence[str],
    *,
    stat: bool = False,
    timeout: float = _GIT_TIMEOUT,
) -> str:
    argv = [
        *_hardened(root, runner, timeout),
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
    ]
    if stat:
        argv.append("--stat=100")
    argv.extend([rev, "--", *paths])
    result = _git(runner, root, argv, timeout=timeout)
    if result is None or result.returncode != 0:
        return ""
    return _stdout(result)


def _show_prefix(
    root: Path, runner: Callable[..., object], timeout: float
) -> str:
    """The cwd's path inside the repo, with a trailing slash (or "")."""
    result = _git(
        runner,
        root,
        [*_HARDENED, "rev-parse", "--show-prefix"],
        timeout=timeout,
    )
    if result is None or result.returncode != 0:
        return ""
    return _stdout(result).strip()


def _heads(
    root: Path,
    paths: Sequence[str],
    reader: Callable[[Path], str | None],
    *,
    limit: int = _FILE_HEAD_CHARS,
    label: str = "new file",
    budget: int | None = None,
) -> str:
    """Read each file whole up to the budget, and say what was cut if not.

    A flat 4 KB cap cut an 8 KB new source file and a 13 KB test file
    mid-function, so the reviewer saw a `[file truncated]` marker instead of
    the function it was asked to judge. Files now read whole; a genuinely
    oversized file reports its real size, and files past the contents budget
    are named instead of dropped.
    """
    parts: list[str] = []
    base = root.resolve()
    total = _HEADS_TOTAL_CHARS if budget is None else max(0, budget)
    used = 0
    omitted: list[str] = []
    for rel in paths:
        if used >= total:
            omitted.append(rel)
            continue
        try:
            # Re-resolve at read time: a symlink swap between validation and
            # this read must not redirect the read outside the project.
            target = (base / rel).resolve()
            target.relative_to(base)
        except (OSError, ValueError, RuntimeError):
            continue
        body = reader(target)
        if body is None:
            continue
        # One file may use what is left of the contents budget; the per-call
        # ``limit`` is only an override. Without the subtraction two 100k files
        # under a 160k budget produced 200k and no truncation marker.
        cap = min(limit, max(0, total - used)) if total else 0
        full = body
        body = full[:cap]
        marker = ""
        if len(full) > cap:
            try:
                size = target.stat().st_size
            except OSError:
                size = len(full)
            marker = (
                f"\n[file truncated: first {len(body)} chars of ~{size} bytes "
                "shown; the rest was not reviewed]"
            )
        parts.append(f"--- {rel} ({label}) ---\n{body}{marker}")
        used += len(body)
    if omitted:
        names = ", ".join(omitted[:8])
        more = f", +{len(omitted) - 8} more" if len(omitted) > 8 else ""
        parts.append(
            f"[not shown: {len(omitted)} file(s) over the "
            f"{total}-byte contents budget: {names}{more}]"
        )
    return "\n".join(parts)


def _file_list(
    runner: Callable[..., object],
    root: Path,
    paths: Sequence[str],
    *,
    base: str = "",
    untracked: Sequence[str] = (),
    remaining: Callable[[], float],
    range_names: str = "",
    prior_names: str = "",
    budget: int = 0,
) -> tuple[str, int]:
    """One line per changed file, independent of the patch budget.

    The rewrite this replaced let a truncated patch look like a complete one:
    a claim naming a file the clipped patch no longer contained was escalated
    to "your claim is unsupported". The list is the part of the evidence that
    survives truncation, so it is always emitted and countable -- but it is
    itself budgeted: a list large enough to push the coverage block out would
    make the evidence lie about its own counts, so rows stop at ``budget``
    characters and the rest are returned as a count for the coverage line.
    """
    rows: list[str] = []
    seen: set[str] = set()
    extra = 0
    prior_extra = 0
    used = 0

    def room() -> bool:
        return budget <= 0 or used < budget

    def add(output: str) -> None:
        nonlocal extra, used
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0].strip(), parts[-1].strip()
            if not path or path in seen:
                continue
            row = f"{status} {path}"
            seen.add(path)
            if len(rows) >= _MAX_LISTED_FILES or not room():
                extra += 1
                continue
            rows.append(row)
            used += len(row) + 1

    if range_names:
        add(range_names)
    elif base and remaining() > 0:
        add(_names(runner, root, f"{base}..HEAD", (), min(_GIT_TIMEOUT, remaining())))
    if remaining() > 0:
        add(_names(runner, root, "HEAD", paths, min(_GIT_TIMEOUT, remaining())))
    for rel in untracked:
        if rel in seen:
            continue
        if len(rows) >= _MAX_LISTED_FILES or not room():
            # Untracked overflow must count like tracked overflow does, or
            # the coverage claim reads "read all" while files were dropped.
            extra += 1
            continue
        seen.add(rel)
        row = f"?? {rel}"
        rows.append(row)
        used += len(row) + 1
    if extra:
        rows.append(f"[+{extra} more files]")
    # The earlier-session rows go LAST, under their own budget: a long
    # session's history must never push this turn's own files out of the list
    # the reviewer counts against the claim.
    if prior_names:
        prior_added = 0
        for line in prior_names.splitlines():
            if line.startswith("[+"):
                # A counted overflow row from _bounded_rows: keep it, it says
                # how many earlier-session files are missing by name.
                rows.append(line)
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0].strip(), parts[-1].strip()
            if not path or path in seen:
                continue
            if (
                prior_added >= _MAX_PRIOR_ROWS
                or len(rows) >= (_MAX_LISTED_FILES + _MAX_PRIOR_ROWS)
                or not room()
            ):
                prior_extra += 1
                continue
            row = f"{status} {path}"
            seen.add(path)
            rows.append(row)
            used += len(row) + 1
            prior_added += 1
        if prior_extra:
            rows.append(f"[+{prior_extra} more earlier-session files]")
    return "\n".join(rows), extra + prior_extra


def _names(
    runner: Callable[..., object],
    root: Path,
    rev: str,
    paths: Sequence[str],
    timeout: float,
) -> str:
    argv = [
        *_hardened(root, runner, timeout),
        "diff",
        "--name-status",
        "--no-color",
        rev,
        "--",
        *paths,
    ]
    result = _git(runner, root, argv, timeout=timeout)
    if result is None or result.returncode != 0:
        return ""
    return _stdout(result)


def _bounded_rows(text: str, rows: int) -> str:
    """Keep ``rows`` name rows and count the rest — never cut mid-line."""
    lines = text.splitlines()
    if len(lines) <= rows:
        return text
    omitted = len(lines) - rows
    return "\n".join([*lines[:rows], f"[+{omitted} more earlier-session files]"])


def _other_dirty(
    root: Path,
    runner: Callable[..., object],
    safe: Sequence[str],
    remaining: Callable[[], float],
) -> int:
    """Count dirty worktree paths that are not part of this turn's set.

    A completion claim is written against the whole worktree while the evidence
    is turn-scoped. Without this count the reviewer saw "18 files modified"
    against a 6-file list and reported the difference as work it could not find.
    """
    timeout = min(_GIT_TIMEOUT, remaining())
    if timeout <= 0:
        return 0
    result = _git(
        runner,
        root,
        [
            *_hardened(root, runner, timeout),
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
        ],
        timeout=timeout,
    )
    if result is None or result.returncode != 0:
        return 0
    known = set(safe)
    # git prints repo-root-relative paths; the host reports cwd-relative ones.
    # The same prefix strip `_gather` applies to untracked entries has to be
    # applied here, or a session started in a subdirectory counts its own turn
    # work as foreign.
    prefix = _show_prefix(root, runner, min(_GIT_TIMEOUT, max(0.0, remaining())))
    others = 0
    fields = _stdout(result).split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:  # an empty tail, or a rename origin already consumed
            continue
        status = entry[:2]
        rel = entry[3:].removesuffix("/")
        if prefix and rel.startswith(prefix):
            rel = rel[len(prefix):]
        names = {rel}
        if "R" in status or "C" in status:
            # A rename/copy emits the destination as the entry path and the
            # origin as the next bare NUL field: one path pair, one entry. Read
            # the origin here, or its first three characters get chopped and it
            # is counted as a second, unrelated file.
            origin = fields[index].removesuffix("/") if index < len(fields) else ""
            index += 1
            names.add(origin)
        names.discard("")
        # Either side counting as this turn's work makes the pair turn work.
        if names and names.isdisjoint(known):
            others += 1
            if others >= _MAX_LISTED_FILES:
                break
    return others


def _range_usable(
    runner: Callable[..., object],
    root: Path,
    base: str,
    timeout: float = _GIT_TIMEOUT,
) -> bool:
    """True when ``base`` is a commit and an ancestor of HEAD.

    A rebase, amend, or unrelated base makes ``base..HEAD`` meaningless (or
    huge), so the caller falls back to the worktree-only diff and says so.
    """
    check = _git(
        runner,
        root,
        [*_hardened(root, runner, timeout), "merge-base", "--is-ancestor", base, "HEAD"],
        timeout=timeout,
    )
    return check is not None and check.returncode == 0


def _cap(text: str, chars: int, note: str) -> str:
    """Clip one header block so a huge stat cannot displace the file list."""
    if chars <= 0 or len(text) <= chars:
        return text
    return text[:chars].rstrip() + f"\n[{note}]"


def _stat_cap(limit: int) -> int:
    """How many characters the stat may take of the evidence budget."""
    return min(20_000, max(400, limit // 8))


def _coverage(
    listed: int,
    reported: int,
    limit: int,
    *,
    git: bool,
    truncated: bool,
    others: int = 0,
    prior_count: int = 0,
    outside_count: int = 0,
    actions_count: int = 0,
    outside_dropped: int = 0,
    dropped_count: int = 0,
    dropped_examples: Sequence[str] = (),
    list_omitted: int = 0,
    timed_out: bool = False,
) -> str:
    """State what the evidence covers, so a partial patch cannot read as a lie.

    Without this line the reviewer saw a clipped diff and a claim naming a file
    the clip had removed, and escalated the gap to "your claim is unsupported".
    """
    if prior_count:
        first = (
            f"{listed} changed file(s) listed; {reported} path(s) collected "
            "(this turn's host report plus the session ledger); patch budget "
            f"{limit} chars."
        )
    else:
        first = (
            f"{listed} changed file(s) listed; {reported} path(s) supplied by "
            f"the host; patch budget {limit} chars."
        )
    if list_omitted:
        first += (
            f" {list_omitted} more file(s) did not fit the list's character "
            "budget and are counted only."
        )
    lines = [
        "--- coverage ---",
        first,
    ]
    if prior_count:
        lines.append(
            f"{prior_count} of the listed file(s) were changed in an earlier "
            "turn of this session (session ledger), not in this turn."
        )
    if outside_count:
        lines.append(
            f"{outside_count} reported path(s) lie outside the working "
            "directory; their contents are included as text, not as a diff."
        )
    if outside_dropped:
        lines.append(
            f"{outside_dropped} more path(s) outside the working directory were "
            f"past the {_MAX_OUTSIDE}-file limit and were not read."
        )
    if dropped_count:
        sample = ", ".join(dropped_examples[:5])
        more = f" (first: {sample})" if sample else ""
        lines.append(
            f"{dropped_count} reported path(s) were beyond the {_MAX_PATHS}-path "
            f"limit and were not read{more}."
        )
    if timed_out:
        lines.append(
            "The git time budget ran out while collecting: a section shown as "
            "empty may be missing rather than unchanged."
        )
    if actions_count:
        lines.append(
            f"{actions_count} recorded session action(s) are listed as "
            "corroboration that they ran, not as evidence of what they produced."
        )
    if others:
        # Not "from earlier turns": the set can also fall short of the worktree
        # because the path cap dropped reported paths, and this line must stay
        # true in both cases.
        lines.append(
            f"{others} other path(s) in this worktree are dirty and are not "
            "part of this change set."
        )
    if not git:
        source = (
            "the host's report and the session ledger"
            if prior_count
            else "the host's report"
        )
        lines.append(
            "git was unavailable: file contents were read directly and the "
            f"list above is {source}, not a complete change set."
        )
    if truncated:
        lines.append(
            "The patch below is truncated. The file list and coverage are complete, "
            "but a file whose patch is missing here has NOT been shown to be "
            "unchanged."
        )
    lines.append(
        "The stat and file list cover this turn's paths (the host's report, "
        "plus any commits made during it) and the earlier-session paths named "
        "above, not the whole worktree, and the patch shows only those paths, "
        "so a file with no patch may still have changed. Judge the work from "
        'the list, and report any gap as severity "unverifiable".'
    )
    return "\n".join(lines)


_DIFF_HEADER = re.compile(r"^diff --git a/\S+ b/(.+)$", re.MULTILINE)
_CONTENT_HEADER = re.compile(
    r"^--- (.+?) \((?:new file|contents|read directly|unreadable)\) ---$",
    re.MULTILINE,
)
_SECTION_HEADER = re.compile(r"^--- .+ ---$", re.MULTILINE)


def _block_names(text: str) -> list[str]:
    """File names a block of evidence announces, in order."""
    names = [match.group(1).strip() for match in _DIFF_HEADER.finditer(text)]
    names.extend(match.group(1).strip() for match in _CONTENT_HEADER.finditer(text))
    return [name for name in names if name]


def _clipped_names(shown: str, tail: str) -> list[str]:
    """Files whose content the clip removed, including the straddling one.

    The reviewer can accept a coverage limit it can name; it cannot review a
    block that stops mid-function without knowing which file that was.
    """
    names: list[str] = []
    stripped = tail.lstrip("\n")
    first_line = stripped.splitlines()[0] if stripped else ""
    at_boundary = (
        not stripped
        or first_line.startswith("diff --git ")
        or bool(_CONTENT_HEADER.match(first_line))
        or bool(_SECTION_HEADER.match(first_line))
    )
    if not at_boundary:
        partial = _block_names(shown)
        if partial:
            names.append(partial[-1])
    names.extend(_block_names(tail))
    return list(dict.fromkeys(names))


def _assemble(header: Sequence[str], body: Sequence[str], limit: int) -> str:
    """Join the sections: stat, file list, coverage, then the patch.

    Sections are ordered stat → list → coverage and trimmed from the front, so
    the coverage line — the authoritative completeness statement — survives a
    large stat. A trimmed header says so: it must never drop the file list
    silently while the coverage still counts the files.
    """
    sections = [section for section in header if section]
    head = "\n\n".join(sections)
    if len(head) > limit:
        kept: list[str] = []
        for section in reversed(sections):
            candidate = [section, *kept]
            if len("\n\n".join(candidate)) + 2 <= limit:
                kept = candidate
            else:
                break
        if kept:
            head = "\n\n".join(kept)
            if len(kept) < len(sections):
                head += "\n[header clipped: a section did not fit the budget]"
        elif sections:
            # Nothing fits whole: keep the head of the coverage block — it
            # names the counts — rather than whichever block happens to sit
            # last (the discovery note may follow it).
            coverage = next(
                (
                    section
                    for section in sections
                    if section.startswith("--- coverage ---")
                ),
                sections[-1],
            )
            head = coverage[: max(0, limit - 20)].rstrip() + "\n[clipped]"
    rest = "\n\n".join(section for section in body if section)
    text = f"{head}\n\n{rest}" if rest else head
    if len(text) <= limit:
        return text
    # Name what the clip removed: a reviewer that cannot see a file is fine as
    # long as it is told which file it did not see. The straddling block counts.
    # The note is reserved for first so it never eats the patch it describes,
    # and as many names as fit are shown (a long list must not collapse to the
    # bare marker and lose them all).
    reserve = 400

    def _note_for(cut: int) -> str:
        names = _clipped_names(rest[:cut], rest[cut:])
        if not names:
            return "\n\n[patch truncated]"
        for keep in range(len(names), 0, -1):
            omitted = len(names) - keep
            tail = f", +{omitted} more" if omitted else ""
            note = (
                "\n\n[patch truncated — not shown: "
                + ", ".join(names[:keep])
                + tail
                + "]"
            )
            if len(note) <= reserve:
                return note
        return "\n\n[patch truncated]"

    note = _note_for(max(0, limit - len(head) - 2 - reserve))
    budget = max(0, limit - len(head) - len(note) - 2)
    note = _note_for(budget)
    budget = max(0, limit - len(head) - len(note) - 2)
    if budget > 0 and rest:
        return f"{head}\n\n{rest[:budget].rstrip()}{note}"
    if not head:
        return text[:limit]
    keep = max(0, limit - len(note))
    # Coverage is the completeness statement; keep its head, never the head of
    # the joined sections (which may be the stat or the list).
    coverage = next(
        (section for section in sections if section.startswith("--- coverage ---")),
        head,
    )
    return (coverage[:keep].rstrip() + note) if keep else note.lstrip()


@dataclass(frozen=True)
class _Gathered:
    """What git could tell us about one change set, or why it could not."""

    committed: str
    uncommitted: str
    new_files: str
    base_ok: bool
    base_note: str
    untracked: tuple[str, ...] = ()
    range_names: str = ""
    # Commits made in EARLIER turns of this session: outside the turn range,
    # and the reason a multi-turn completion claim used to look unsupported.
    prior_committed: str = ""
    prior_names: str = ""


def _gather(
    root: Path,
    safe: Sequence[str],
    base: str,
    runner: Callable[..., object],
    reader: Callable[[Path], str | None],
    remaining: Callable[[], float],
    *,
    prior: Sequence[str] = (),
    session_base: str = "",
    budget: int | None = None,
) -> _Gathered | None:
    """Run the git queries; ``None`` means git is unusable here."""
    status = _git(
        runner,
        root,
        [
            *_hardened(root, runner, min(_GIT_TIMEOUT, remaining())),
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--",
            *safe,
        ],
        timeout=min(_GIT_TIMEOUT, remaining()),
    )
    if status is None or status.returncode != 0:
        return None

    prefix = _show_prefix(root, runner, min(_GIT_TIMEOUT, remaining()))

    uncommitted = _diff(
        runner, root, "HEAD", safe, timeout=min(_GIT_TIMEOUT, remaining())
    )
    safe_set = set(safe)
    untracked: list[str] = []
    for entry in _stdout(status).split("\0"):
        if not entry.startswith("?? "):
            continue
        rel = entry[3:].removesuffix("/")
        # git prints repo-root-relative paths; the host reports cwd-relative
        # ones. Without the prefix strip a session started in a subdirectory
        # lost every new file it created.
        if prefix and rel.startswith(prefix):
            rel = rel[len(prefix):]
        if rel and rel in safe_set:
            untracked.append(rel)
    new_files = (
        _heads(root, _safe_paths(root, untracked), reader, budget=budget)
        if untracked
        else ""
    )

    committed = ""
    range_names = ""
    base_note = "no turn base recorded"
    base_ok = bool(base) and _range_usable(
        runner, root, base, timeout=min(_GIT_TIMEOUT, remaining())
    )
    if base_ok:
        # The patch follows the host's paths (relevance and size), but the
        # description of the range must not: filtering it once made a 29-file
        # commit look like a 2-file commit, and the reviewer rightly called the
        # claim unsupported.
        committed = _diff(
            runner, root, f"{base}..HEAD", safe, timeout=min(_GIT_TIMEOUT, remaining())
        )
        range_names = _names(
            runner,
            root,
            f"{base}..HEAD",
            (),
            min(_GIT_TIMEOUT, remaining()),
        )
        if committed or range_names:
            base_note = f"commits since turn start ({base[:12]}..HEAD)"
    elif base:
        base_note = "turn base is not an ancestor of HEAD; worktree only"

    # Work committed in an earlier turn of this session. The turn range cannot
    # show it (it starts at this turn's HEAD), so a completion claim covering
    # several turns had its earlier commits reviewed as missing work.
    prior_committed = ""
    prior_names = ""
    prior_set = set(prior)
    prior_safe = [path for path in safe if path in prior_set]
    if prior_safe and session_base and _range_usable(
        runner, root, session_base, timeout=min(_GIT_TIMEOUT, remaining())
    ):
        rev = (
            f"{session_base}..{base}"
            if base_ok and base and base != session_base
            else f"{session_base}..HEAD"
        )
        prior_committed = _cap(
            _diff(runner, root, rev, prior_safe, timeout=min(_GIT_TIMEOUT, remaining())),
            _PRIOR_PATCH_CHARS,
            "earlier-session diff truncated",
        )
        prior_names = _bounded_rows(
            _names(runner, root, rev, (), min(_GIT_TIMEOUT, remaining())),
            _MAX_PRIOR_ROWS,
        )
    return _Gathered(
        committed=committed,
        uncommitted=uncommitted,
        new_files=new_files,
        base_ok=base_ok,
        base_note=base_note,
        untracked=tuple(untracked),
        range_names=range_names,
        prior_committed=prior_committed,
        prior_names=prior_names,
    )


def _collect_discovered(
    root: Path,
    safe: Sequence[str],
    runner: Callable[..., object],
    reader: Callable[[Path], str | None],
    remaining: Callable[[], float],
    *,
    budget: int | None = None,
) -> tuple[_Gathered | None, Path, list[str], str]:
    """Find the repo the reported paths actually live in and gather there.

    git is not usable under the plugin's cwd when the agent works in a project
    below it. Returns the gathered evidence (or None), the root and paths to
    use, and the note that tells the reviewer which prefix the paths carry.
    """
    found, rels, others = _discover_repo(root, safe, runner, remaining)
    if found == root or not rels:
        return None, root, list(safe), ""
    gathered = _gather(found, rels, "", runner, reader, remaining, budget=budget)
    if gathered is None:
        return None, root, list(safe), ""
    try:
        prefix = str(found.relative_to(root).as_posix()) + "/"
    except (OSError, ValueError, RuntimeError):
        prefix = ""
    extra = (
        "Paths outside this repo were listed but not diffed:\n"
        + "\n".join(others[:10])
        if others
        else ""
    )
    mapping = (
        f"Report these repo-relative paths against the claim with the\n"
        f"prefix `{prefix}` (the report named them under the plugin's).\n"
        if prefix
        else "The report named these paths by absolute location.\n"
    )
    head = _DISCOVERY_NOTE.format(
        root=found,
        extra=mapping + (f"{extra}\n" if extra else ""),
    )
    return gathered, found, list(rels), head


def _outside_paths(root: Path, paths: Sequence[str]) -> tuple[list[str], int]:
    """Absolute reported paths outside the working directory, plus overflow.

    The host reports absolute paths. The gate used to drop every one outside
    the plugin's cwd, so a skill file edited from a project session vanished
    from the change set and the reviewer called the claim unverifiable. These
    are read as content only -- never diffed -- and only because the host
    reported them. Past the cap, the overflow is counted so it can be named.
    """
    try:
        base = root.resolve()
    except (OSError, ValueError, RuntimeError):
        return [], 0
    found: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for raw in paths or ():
        text = str(raw or "").strip()
        if not text:
            continue
        candidate = Path(text)
        if not candidate.is_absolute() and ".." not in candidate.parts:
            continue
        try:
            # A relative path resolves against the working directory we were
            # given, never against the process cwd.
            target = (
                candidate.resolve()
                if candidate.is_absolute()
                else (base / candidate).resolve()
            )
        except (OSError, ValueError, RuntimeError):
            continue
        try:
            target.relative_to(base)
            continue  # inside: the git/diff pipeline owns it
        except (OSError, ValueError):
            pass
        if str(target) in seen or not target.is_file():
            continue
        seen.add(str(target))
        if len(found) >= _MAX_OUTSIDE:
            dropped += 1
            continue
        found.append(str(target))
    return found, dropped


def _outside_text(
    paths: Sequence[str],
    reader: Callable[[Path], str | None],
    budget: int | None = None,
) -> str:
    """Contents of reported paths outside cwd, bounded and labelled."""
    parts = ["--- outside the working directory (contents, not a diff) ---"]
    used = 0
    for path in paths:
        target = Path(path)
        body = reader(target)
        if body is None:
            parts.append(f"--- {path} (unreadable) ---")
            continue
        cap = len(body) if budget is None else max(0, budget - used)
        full = body
        body = full[:cap]
        marker = ""
        if len(full) > len(body):
            try:
                size = target.stat().st_size
            except OSError:
                size = len(full)
            marker = (
                f"\n[file truncated: first {len(body)} chars of ~{size} bytes "
                "shown; the rest was not reviewed]"
            )
        parts.append(f"--- {path} (read directly) ---\n{body}{marker}")
        used += len(body)
    return "\n".join(parts)


def _git_unavailable(
    root: Path,
    safe: Sequence[str],
    reader: Callable[[Path], str | None],
    limit: int,
    *,
    prior_count: int = 0,
    outside: Sequence[str] = (),
    actions: Sequence[str] = (),
    dropped_count: int = 0,
    dropped_examples: Sequence[str] = (),
    outside_dropped: int = 0,
    timed_out: bool = False,
) -> str:
    """File heads only: say what was read and that the list is the host's."""
    read_files = _heads(root, safe, reader, label="contents", budget=limit)
    listing = "--- files (git unavailable; host-reported paths) ---\n" + "\n".join(
        f"? {rel}" for rel in safe
    )
    coverage = _coverage(
        len(safe),
        len(safe),
        limit,
        git=False,
        truncated=len(read_files) > limit,
        prior_count=prior_count,
        outside_count=len(outside),
        dropped_count=dropped_count,
        dropped_examples=dropped_examples,
        outside_dropped=outside_dropped,
        timed_out=timed_out,
    )
    body = [read_files]
    if outside:
        body.append(_outside_text(outside, reader, budget=limit))
    if actions:
        body.append(_actions_text(actions))
    return _assemble([listing, coverage], body, limit)


def _actions_text(actions: Sequence[str]) -> str:
    """Session actions that left no diff, as corroboration only."""
    lines = [
        "--- recorded session actions (corroboration, not evidence of their "
        "result) ---"
    ]
    lines.extend(f"· {line}" for line in actions if str(line).strip())
    return "\n".join(lines)


def _empty_change_set(remaining: Callable[[], float], limit: int) -> str:
    """Distinguish 'nothing changed' from 'the git budget ran out'."""
    if remaining() <= 0:
        return _assemble(
            [
                "--- coverage ---\n"
                "Evidence collection ran out of its git budget before the "
                "change set could be read. Treat the work as unverified, not "
                'unchanged, and report the gap as severity "unverifiable".'
            ],
            [],
            limit,
        )
    # Nothing changed: reviewing nothing is worse than skipping, and the
    # caller turns an empty result into "no review".
    return ""


def _evidence_text(
    gathered: _Gathered,
    files: str,
    stat: str,
    listed: int,
    safe: Sequence[str],
    others: int,
    discovery_head: str,
    limit: int,
    *,
    prior_text: str = "",
    prior_count: int = 0,
    outside_text: str = "",
    outside_count: int = 0,
    actions_text: str = "",
    actions_count: int = 0,
    dropped_count: int = 0,
    dropped_examples: Sequence[str] = (),
    list_omitted: int = 0,
    outside_dropped: int = 0,
    timed_out: bool = False,
) -> str:
    """Assemble the stat, file list, coverage and patch under one budget."""
    body: list[str] = []
    if gathered.committed:
        body.append(f"--- committed during this turn ---\n{gathered.committed.rstrip()}")
    # This turn's own work comes before any earlier-session history: the block
    # is clipped from the tail, and a long session range must not push the
    # turn's patch out of the evidence.
    if gathered.uncommitted:
        body.append(f"--- uncommitted ---\n{gathered.uncommitted.rstrip()}")
    if gathered.new_files:
        body.append(f"--- new files ---\n{gathered.new_files.rstrip()}")
    if gathered.prior_committed:
        body.append(
            "--- committed earlier in this session ---\n"
            f"{gathered.prior_committed.rstrip()}"
        )
    if prior_text:
        body.append(prior_text)
    if outside_text:
        body.append(outside_text)
    if actions_text:
        body.append(actions_text)

    header: list[str] = []
    if stat:
        header.append(
            f"--- stat ({gathered.base_note}) ---\n"
            f"{_cap(stat.rstrip(), _stat_cap(limit), 'stat truncated')}"
        )
    if files:
        header.append(f"--- files changed ({listed}) ---\n{files}")
    # The coverage sentence is the authoritative completeness statement, so it
    # has to describe the assembly that actually ships. Build once with the
    # warning present, and only downgrade to "complete" when the patch really
    # did fit.
    header.append(
        _coverage(
            listed,
            len(safe),
            limit,
            git=True,
            truncated=True,
            others=others,
            prior_count=prior_count,
            outside_count=outside_count,
            actions_count=actions_count,
            dropped_count=dropped_count,
            dropped_examples=dropped_examples,
            list_omitted=list_omitted,
            outside_dropped=outside_dropped,
            timed_out=timed_out,
        )
    )
    if discovery_head:
        header.append(discovery_head)
    probe = _assemble(header, body, limit)
    if "[patch truncated" in probe:
        return probe
    header[-2 if discovery_head else -1] = _coverage(
        listed,
        len(safe),
        limit,
        git=True,
        truncated=False,
        others=others,
        prior_count=prior_count,
        outside_count=outside_count,
        actions_count=actions_count,
        dropped_count=dropped_count,
        dropped_examples=dropped_examples,
        list_omitted=list_omitted,
        outside_dropped=outside_dropped,
        timed_out=timed_out,
    )
    return _assemble(header, body, limit)


def collect(
    cwd: str,
    paths: Sequence[str],
    *,
    base: str = "",
    prior: Sequence[str] = (),
    actions: Sequence[str] = (),
    session_base: str = "",
    max_chars: int = 8000,
    run: Callable[..., object] | None = None,
    read: Callable[[Path], str | None] | None = None,
    clock: Callable[[], float] | None = None,
) -> str:
    """Return the changed-file evidence for ``paths`` under ``cwd``.

    ``base`` is the commit HEAD pointed at when the turn started. When it is a
    usable ancestor, the evidence is prefixed with the commits made during the
    turn, which is where a completion claim's work usually lives after the
    agent commits. ``prior`` names paths changed in EARLIER turns of the same
    session and ``session_base`` is HEAD at the session's first turn: together
    they extend the evidence past the turn boundary, because the host's
    ``changed_paths`` is reset every turn and a multi-turn claim otherwise
    reads to the reviewer as work that is missing.

    Git calls share one overall budget (``_GIT_TOTAL_BUDGET``): each call gets
    at most ``_GIT_TIMEOUT`` and whatever is left. Hung git plus the Review
    model call must stay under the host's hook timeout, so the budget
    degrades to partial evidence instead of a killed hook.

    The output always leads with the stat, a complete file list and a coverage
    line; only the patch is clipped. A partial patch therefore cannot read as
    a complete one.
    """
    root = Path(cwd)
    valid = _safe_paths(root, paths)
    safe = valid[:_MAX_PATHS]
    # Exact: every accepted path beyond the cap, not "inputs minus accepted"
    # (which counted duplicates and refused paths as if the cap dropped them).
    dropped = max(0, len(valid) - len(safe))
    outside, outside_dropped = _outside_paths(root, paths)
    if not safe:
        if not outside:
            return ""
        reader = read or _read_text
        body = [_outside_text(outside, reader, budget=max(0, int(max_chars)))]
        if actions:
            body.append(_actions_text(actions))
        return _assemble(
            [
                _coverage(
                    0,
                    0,
                    max(0, int(max_chars)),
                    git=False,
                    truncated=False,
                    outside_count=len(outside),
                    outside_dropped=outside_dropped,
                    actions_count=len(actions),
                )
            ],
            body,
            max(0, int(max_chars)),
        )
    runner = run or subprocess.run
    reader = read or _read_text
    now = clock or time.monotonic
    deadline = now() + _GIT_TOTAL_BUDGET
    limit = max(0, int(max_chars))

    def remaining() -> float:
        return max(0.0, deadline - now())

    base = str(base or "").strip()
    session_base = str(session_base or "").strip()
    # The ledger holds absolute paths; git wants the same relative form as the
    # report, so normalize through the same containment check.
    prior_safe = [path for path in _safe_paths(root, prior) if path in set(safe)]
    gathered = _gather(
        root,
        safe,
        base,
        runner,
        reader,
        remaining,
        prior=prior_safe,
        session_base=session_base,
        budget=limit,
    )
    use_root, use_safe = root, safe
    discovery_head = ""
    if gathered is None:
        # git is not usable under the plugin's cwd. The change set usually
        # still lives in a repo beneath it (agent cwd = home directory,
        # project = ~/Development/...): find that repo from the reported
        # paths and collect there instead of degrading to file-head reads.
        gathered, use_root, use_safe, discovery_head = _collect_discovered(
            root, safe, runner, reader, remaining, budget=limit
        )
        # The turn base was snapshotted in the plugin's repo; it is
        # meaningless for a discovered one.
        if gathered is not None:
            base = ""
            prior_safe = [
                path for path in _safe_paths(use_root, prior) if path in set(use_safe)
            ]
    if gathered is None:
        # git is unusable here: still say what was read and that the list is
        # the host's report, never a complete change set.
        return _git_unavailable(
            root,
            safe,
            reader,
            limit,
            prior_count=len(prior_safe),
            outside=outside,
            actions=actions,
            dropped_count=dropped,
            dropped_examples=valid[_MAX_PATHS:][:5],
            outside_dropped=outside_dropped,
            timed_out=remaining() <= 0,
        )

    if not (
        gathered.committed
        or gathered.uncommitted
        or gathered.new_files
        or gathered.range_names
        or gathered.prior_committed
        or gathered.prior_names
        or outside
    ):
        return _empty_change_set(remaining, limit)

    files, list_omitted = _file_list(
        runner,
        use_root,
        use_safe,
        base=base if gathered.base_ok else "",
        untracked=gathered.untracked,
        remaining=remaining,
        range_names=gathered.range_names,
        prior_names=gathered.prior_names,
        # The list must never push the coverage block out of the header: that
        # is what made a trimmed head say "N listed" with no list in sight.
        budget=max(300, limit - min(2500, max(600, limit // 4)) - _stat_cap(limit)),
    )
    listed = sum(1 for line in files.splitlines() if not line.startswith("[+"))
    # Scope transparency: the turn's set is a subset of the worktree, and a
    # claim that counts the tree must not read as work this turn never did.
    others = _other_dirty(use_root, runner, use_safe, remaining)
    if gathered.base_ok:
        # Unconditional: the range's description must never fall back to the
        # host's path list, or a commit again reads as smaller than it is.
        stat = _diff(
            runner,
            use_root,
            f"{base}..HEAD",
            (),
            stat=True,
            timeout=min(_GIT_TIMEOUT, remaining()),
        )
    else:
        stat = _diff(
            runner, use_root, "HEAD", use_safe, stat=True,
            timeout=min(_GIT_TIMEOUT, remaining()),
        )

    # Count only the earlier-turn paths that really made it into the list; a
    # claim must never be judged against a count the list cannot show.
    prior_listed = 0
    if prior_safe:
        rows = {line.split(" ", 1)[-1].strip() for line in files.splitlines()}
        prior_listed = sum(1 for path in prior_safe if path in rows)
    prior_text = ""
    if prior_safe and not gathered.prior_committed and not gathered.uncommitted:
        # No diff path for the earlier work (clean worktree, no session range):
        # show the contents rather than silence.
        prior_text = (
            "--- earlier-turn files (contents; no diff available) ---\n"
            f"{_heads(use_root, prior_safe, reader, label='contents', budget=limit)}"
        )
    outside_text = _outside_text(outside, reader, budget=limit) if outside else ""
    actions_text = _actions_text(actions) if actions else ""

    return _evidence_text(
        gathered,
        files,
        stat,
        listed,
        safe,
        others,
        discovery_head,
        limit,
        prior_text=prior_text,
        prior_count=prior_listed,
        outside_text=outside_text,
        outside_count=len(outside),
        actions_text=actions_text,
        actions_count=len(actions),
        dropped_count=dropped,
        dropped_examples=valid[_MAX_PATHS:][:5],
        list_omitted=list_omitted,
        outside_dropped=outside_dropped,
        timed_out=remaining() <= 0,
    )
