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
from dataclasses import dataclass
from collections.abc import Callable, Sequence
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
                safe.append(candidate.resolve().relative_to(base).as_posix())
            except (OSError, ValueError, RuntimeError):
                continue
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
        add(_names(runner, root, f"{base}..HEAD", (), remaining()))
    if remaining() > 0:
        add(_names(runner, root, "HEAD", paths, remaining()))
    for rel in untracked:
        if rel not in seen and len(rows) < _MAX_LISTED_FILES:
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
        "The stat and file list describe every file changed since the turn "
        "started; the patch shows only the paths the host reported, so a file "
        "with no patch may still have changed. Judge the work from the list, "
        "and report any gap as severity \"unverifiable\"."
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
            # Nothing fits whole: keep the head of the highest-priority block
            # (the coverage line names the counts) rather than nothing at all.
            head = sections[-1][: max(0, limit - 20)].rstrip() + "\n[clipped]"
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
            runner, root, f"{base}..HEAD", (), remaining()
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
        # Nothing changed: reviewing nothing is worse than skipping, and the
        # caller turns an empty result into "no review".
        return ""

    files = _file_list(
        runner,
        root,
        safe,
        base=base if gathered.base_ok else "",
        untracked=gathered.untracked,
        remaining=remaining,
        range_names=gathered.range_names,
    )
    listed = len(files.splitlines())
    if gathered.base_ok:
        # Unconditional: the range's description must never fall back to the
        # host's path list, or a commit again reads as smaller than it is.
        stat = _diff(
            runner,
            root,
            f"{base}..HEAD",
            (),
            stat=True,
            timeout=min(_GIT_TIMEOUT, remaining()),
        )
    else:
        stat = _diff(
            runner, root, "HEAD", safe, stat=True,
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
    header.append(_coverage(listed, len(safe), limit, git=True, truncated=truncated))
    return _assemble(header, body, limit)
