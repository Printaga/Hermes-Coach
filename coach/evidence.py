"""Bounded diff evidence for the Review gate.

``collect`` asks git for the change set under the supplied paths and falls
back to reading the files directly when git is unavailable or fails. Paths are
project-relative: absolute paths and any ``..`` component are refused so the
hook cannot read outside the working directory.

Evidence covers both halves of a turn's work, because agents routinely commit
before claiming completion: commits made since the turn started (``base``
supplied by the caller) plus whatever is still uncommitted. A git ``--stat``
line leads the block so a size cap degrades to a summary instead of hiding
which files changed.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["collect", "head"]

_GIT_TIMEOUT = 2
_GIT_TOTAL_BUDGET = 8.0
_MAX_LISTED_FILES = 200
_FILE_HEAD_CHARS = 4000
_MAX_PATHS = 300
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
            if len(safe) >= _MAX_PATHS:
                break
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
        if len(safe) >= _MAX_PATHS:
            break
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
) -> str:
    parts: list[str] = []
    base = root.resolve()
    for rel in paths:
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
        marker = "\n[file truncated]" if len(body) >= limit else ""
        parts.append(f"--- {rel} (new file) ---\n{body}{marker}")
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
) -> str:
    """One line per changed file, independent of the patch budget.

    The rewrite this replaced let a truncated patch look like a complete one:
    a claim naming a file the clipped patch no longer contained was escalated
    to "your claim is unsupported". The list is the part of the evidence that
    survives truncation, so it is always emitted, complete and countable.
    """
    rows: list[str] = []
    seen: set[str] = set()

    extra = 0

    def add(output: str) -> None:
        nonlocal extra
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0].strip(), parts[-1].strip()
            if not path or path in seen:
                continue
            seen.add(path)
            if len(rows) >= _MAX_LISTED_FILES:
                extra += 1
                continue
            rows.append(f"{status} {path}")

    if range_names:
        add(range_names)
    elif base and remaining() > 0:
        add(_names(runner, root, f"{base}..HEAD", (), min(_GIT_TIMEOUT, remaining())))
    if remaining() > 0:
        add(_names(runner, root, "HEAD", paths, min(_GIT_TIMEOUT, remaining())))
    for rel in untracked:
        if rel in seen:
            continue
        if len(rows) >= _MAX_LISTED_FILES:
            # Untracked overflow must count like tracked overflow does, or
            # the coverage claim reads "read all" while files were dropped.
            extra += 1
            continue
        seen.add(rel)
        rows.append(f"?? {rel}")
    if extra:
        rows.append(f"[+{extra} more files]")
    return "\n".join(rows)


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


def _coverage(
    listed: int,
    reported: int,
    limit: int,
    *,
    git: bool,
    truncated: bool,
    others: int = 0,
) -> str:
    """State what the evidence covers, so a partial patch cannot read as a lie.

    Without this line the reviewer saw a clipped diff and a claim naming a file
    the clip had removed, and escalated the gap to "your claim is unsupported".
    """
    lines = [
        "--- coverage ---",
        (
            f"{listed} changed file(s) listed; {reported} path(s) supplied by "
            f"the host; patch budget {limit} chars."
        ),
    ]
    if others:
        lines.append(
            f"{others} other path(s) in this worktree are dirty from earlier "
            "turns and are not part of this change set."
        )
    if not git:
        lines.append(
            "git was unavailable: file contents were read directly and the list "
            "above is the host's report, not a complete change set."
        )
    if truncated:
        lines.append(
            "The patch below is truncated. The file list and coverage are complete, "
            "but a file whose patch is missing here has NOT been shown to be "
            "unchanged."
        )
    lines.append(
        "The stat and file list cover this turn's paths (the host's report, "
        "plus any commits made during it), not the whole worktree, and the "
        "patch shows only the paths the host reported, so a file with no patch "
        "may still have changed. Judge the work from the list, and report any "
        "gap as severity \"unverifiable\"."
    )
    return "\n".join(lines)


def _assemble(header: Sequence[str], body: Sequence[str], limit: int) -> str:
    """Join the sections: coverage, then the file list, then the stat.

    When even the header cannot fit, the least load-bearing block goes first —
    a stat is a convenience, the list and the coverage line are the evidence.
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
    note = "\n\n[patch truncated]"
    budget = limit - len(head) - len(note) - 2
    if budget > 0 and rest:
        return f"{head}\n\n{rest[:budget].rstrip()}{note}"
    if not head:
        return text[:limit]
    keep = max(0, limit - len(note))
    # Sections are ordered stat → list → coverage, so the tail is the part
    # that must survive; keeping the prefix dropped the coverage line.
    return (head[len(head) - keep :].lstrip() + note) if keep else note.lstrip()


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


def _gather(
    root: Path,
    safe: Sequence[str],
    base: str,
    runner: Callable[..., object],
    reader: Callable[[Path], str | None],
    remaining: Callable[[], float],
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
    new_files = _heads(root, _safe_paths(root, untracked), reader) if untracked else ""

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
    return _Gathered(
        committed=committed,
        uncommitted=uncommitted,
        new_files=new_files,
        base_ok=base_ok,
        base_note=base_note,
        untracked=tuple(untracked),
        range_names=range_names,
    )


def collect(
    cwd: str,
    paths: Sequence[str],
    *,
    base: str = "",
    max_chars: int = 8000,
    run: Callable[..., object] | None = None,
    read: Callable[[Path], str | None] | None = None,
    clock: Callable[[], float] | None = None,
) -> str:
    """Return the changed-file evidence for ``paths`` under ``cwd``.

    ``base`` is the commit HEAD pointed at when the turn started. When it is a
    usable ancestor, the evidence is prefixed with the commits made during the
    turn, which is where a completion claim's work usually lives after the
    agent commits. Without a usable base the evidence is the worktree alone.

    Git calls share one overall budget (``_GIT_TOTAL_BUDGET``): each call gets
    at most ``_GIT_TIMEOUT`` and whatever is left. Hung git plus the Review
    model call must stay under the host's hook timeout, so the budget
    degrades to partial evidence instead of a killed hook.

    The output always leads with the stat, a complete file list and a coverage
    line; only the patch is clipped. A partial patch therefore cannot read as a
    complete one.
    """
    root = Path(cwd)
    safe = _safe_paths(root, paths)
    if not safe:
        return ""
    runner = run or subprocess.run
    reader = read or _read_text
    now = clock or time.monotonic
    deadline = now() + _GIT_TOTAL_BUDGET
    limit = max(0, int(max_chars))

    def remaining() -> float:
        return max(0.0, deadline - now())

    base = str(base or "").strip()
    gathered = _gather(root, safe, base, runner, reader, remaining)
    use_root, use_safe = root, safe
    discovery_head = ""
    if gathered is None:
        # git is not usable under the plugin's cwd. The change set usually
        # still lives in a repo beneath it (agent cwd = home directory,
        # project = ~/Development/...): find that repo from the reported
        # paths and collect there instead of degrading to file-head reads.
        found, rels, others = _discover_repo(root, safe, runner, remaining)
        if found != root and rels:
            gathered = _gather(found, rels, "", runner, reader, remaining)
            if gathered is not None:
                use_root, use_safe = found, rels
                # The turn base was snapshotted in the plugin's repo; it is
                # meaningless for a discovered one.
                base = ""
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
                discovery_head = _DISCOVERY_NOTE.format(
                    root=found,
                    extra=mapping + (f"{extra}\n" if extra else ""),
                )
    if gathered is None:
        # git is unusable here: still say what was read and that the list is
        # the host's report, never a complete change set.
        read_files = _heads(root, safe, reader)
        listing = "--- files (git unavailable; host-reported paths) ---\n" + "\n".join(
            f"? {rel}" for rel in safe
        )
        coverage = _coverage(
            len(safe), len(safe), limit, git=False, truncated=len(read_files) > limit
        )
        return _assemble([listing, coverage], [read_files], limit)

    if not (
        gathered.committed
        or gathered.uncommitted
        or gathered.new_files
        or gathered.range_names
    ):
        # An empty change set and an exhausted git budget look identical here:
        # _diff/_names return "" for a failure and for "nothing to report".
        # Silence is only honest when the queries ran; otherwise the gate reads
        # a timeout as "nothing changed" and skips a turn that did change files.
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

    files = _file_list(
        runner,
        use_root,
        use_safe,
        base=base if gathered.base_ok else "",
        untracked=gathered.untracked,
        remaining=remaining,
        range_names=gathered.range_names,
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

    body: list[str] = []
    if gathered.committed:
        body.append(f"--- committed during this turn ---\n{gathered.committed.rstrip()}")
    if gathered.uncommitted:
        body.append(f"--- uncommitted ---\n{gathered.uncommitted.rstrip()}")
    if gathered.new_files:
        body.append(f"--- new files ---\n{gathered.new_files.rstrip()}")

    header: list[str] = []
    if stat:
        header.append(
            f"--- stat ({gathered.base_note}) ---\n"
            f"{_cap(stat.rstrip(), max(400, limit // 2), 'stat truncated')}"
        )
    if files:
        header.append(f"--- files changed ({listed}) ---\n{files}")
    rest = "\n\n".join(body)
    truncated = bool(rest) and (sum(len(section) for section in header) + len(rest) + 48 > limit)
    header.append(_coverage(listed, len(safe), limit, git=True, truncated=truncated, others=others))
    if discovery_head:
        header.append(discovery_head)
    return _assemble(header, body, limit)
