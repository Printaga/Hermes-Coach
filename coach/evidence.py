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
        safe.append(candidate.as_posix())
    return safe


def _git(
    run: Callable[..., object],
    root: Path,
    argv: list[str],
    timeout: float = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess | None:
    try:
        result = run(
            argv,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=max(0.2, timeout),
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
        *_HARDENED,
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
) -> str:
    """One line per changed file, independent of the patch budget.

    The rewrite this replaced let a truncated patch look like a complete one:
    a claim naming a file the clipped patch no longer contained was escalated
    to "your claim is unsupported". The list is the part of the evidence that
    survives truncation, so it is always emitted, complete and countable.
    """
    rows: list[str] = []
    seen: set[str] = set()

    def add(output: str) -> None:
        if len(rows) >= _MAX_LISTED_FILES:
            return
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0].strip(), parts[-1].strip()
            if not path or path in seen:
                continue
            seen.add(path)
            rows.append(f"{status} {path}")
            if len(rows) >= _MAX_LISTED_FILES:
                return

    if base and remaining() > 0:
        add(_names(runner, root, f"{base}..HEAD", paths, remaining()))
    if remaining() > 0:
        add(_names(runner, root, "HEAD", paths, remaining()))
    for rel in untracked:
        if rel not in seen and len(rows) < _MAX_LISTED_FILES:
            seen.add(rel)
            rows.append(f"?? {rel}")
    return "\n".join(rows)


def _names(
    runner: Callable[..., object],
    root: Path,
    rev: str,
    paths: Sequence[str],
    timeout: float,
) -> str:
    argv = [*_HARDENED, "diff", "--name-status", "--no-color", rev, "--", *paths]
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
        [*_HARDENED, "merge-base", "--is-ancestor", base, "HEAD"],
        timeout=timeout,
    )
    return check is not None and check.returncode == 0


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
            "The patch below is truncated. The stat, file list and coverage are "
            "complete, but a file whose patch is missing here has NOT been shown "
            "to be unchanged."
        )
    return "\n".join(lines)


def _assemble(header: Sequence[str], body: Sequence[str], limit: int) -> str:
    """Join the sections, keeping the header whole when the patch must clip."""
    head = "\n\n".join(section for section in header if section)
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
    return head[:keep].rstrip() + note


@dataclass(frozen=True)
class _Gathered:
    """What git could tell us about one change set, or why it could not."""

    committed: str
    uncommitted: str
    new_files: str
    base_ok: bool
    base_note: str
    untracked: tuple[str, ...] = ()


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
            *_HARDENED,
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

    uncommitted = _diff(
        runner, root, "HEAD", safe, timeout=min(_GIT_TIMEOUT, remaining())
    )
    safe_set = set(safe)
    untracked: list[str] = []
    for entry in _stdout(status).split("\0"):
        if not entry.startswith("?? "):
            continue
        rel = entry[3:].removesuffix("/")
        if rel and rel in safe_set:
            untracked.append(rel)
    new_files = _heads(root, _safe_paths(root, untracked), reader) if untracked else ""

    committed = ""
    base_note = "no turn base recorded"
    base_ok = bool(base) and _range_usable(
        runner, root, base, timeout=min(_GIT_TIMEOUT, remaining())
    )
    if base_ok:
        committed = _diff(
            runner, root, f"{base}..HEAD", safe, timeout=min(_GIT_TIMEOUT, remaining())
        )
        if committed:
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

    if not (gathered.committed or gathered.uncommitted or gathered.new_files):
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
    )
    listed = len(files.splitlines())
    stat = (
        _diff(
            runner,
            root,
            f"{base}..HEAD",
            safe,
            stat=True,
            timeout=min(_GIT_TIMEOUT, remaining()),
        )
        if gathered.committed
        else _diff(runner, root, "HEAD", safe, stat=True, timeout=min(_GIT_TIMEOUT, remaining()))
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
        header.append(f"--- stat ({gathered.base_note}) ---\n{stat.rstrip()}")
    if files:
        header.append(f"--- files changed ({listed}) ---\n{files}")
    rest = "\n\n".join(body)
    truncated = bool(rest) and (sum(len(section) for section in header) + len(rest) + 48 > limit)
    header.append(_coverage(listed, len(safe), limit, git=True, truncated=truncated))
    return _assemble(header, body, limit)
