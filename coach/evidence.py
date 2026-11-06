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
from collections.abc import Callable, Sequence
from pathlib import Path

__all__ = ["collect", "head"]

_GIT_TIMEOUT = 5
_HARDENED = [
    "git",
    "-c",
    "core.quotePath=false",
    # Repo config must not colour the patch, invoke an external diff, or run
    # a textconv driver from a repository the agent does not control.
    "--no-pager",
]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
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
) -> subprocess.CompletedProcess | None:
    try:
        result = run(
            argv,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
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
    result = _git(runner, root, argv)
    if result is None or result.returncode != 0:
        return ""
    return _stdout(result)


def _heads(
    root: Path,
    paths: Sequence[str],
    reader: Callable[[Path], str | None],
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
        parts.append(f"--- {rel} (new file) ---\n{body}")
    return "\n".join(parts)


def _range_usable(runner: Callable[..., object], root: Path, base: str) -> bool:
    """True when ``base`` is a commit and an ancestor of HEAD.

    A rebase, amend, or unrelated base makes ``base..HEAD`` meaningless (or
    huge), so the caller falls back to the worktree-only diff and says so.
    """
    check = _git(
        runner, root, [*_HARDENED, "merge-base", "--is-ancestor", base, "HEAD"]
    )
    return check is not None and check.returncode == 0


def collect(
    cwd: str,
    paths: Sequence[str],
    *,
    base: str = "",
    max_chars: int = 8000,
    run: Callable[..., object] | None = None,
    read: Callable[[Path], str | None] | None = None,
) -> str:
    """Return the changed-file evidence for ``paths`` under ``cwd``.

    ``base`` is the commit HEAD pointed at when the turn started. When it is a
    usable ancestor, the evidence is prefixed with the commits made during the
    turn, which is where a completion claim's work usually lives after the
    agent commits. Without a usable base the evidence is the worktree alone.
    """
    root = Path(cwd)
    safe = _safe_paths(root, paths)
    if not safe:
        return ""
    runner = run or subprocess.run
    reader = read or _read_text

    # -z keeps paths unquoted and unescaped (no C-style octal for non-ASCII).
    # --untracked-files=all lists new files individually instead of collapsing
    # new directories to a trailing-slash "dir/" entry git diff would miss.
    status = _git(
        runner,
        root,
        [
            "git",
            "-c",
            "core.quotePath=false",
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--",
            *safe,
        ],
    )
    if status is None or status.returncode != 0:
        return _heads(root, safe, reader)[: max(0, int(max_chars))]

    uncommitted = _diff(runner, root, "HEAD", safe)
    untracked: list[str] = []
    safe_set = set(safe)
    for entry in _stdout(status).split("\0"):
        if not entry.startswith("?? "):
            continue
        rel = entry[3:].removesuffix("/")
        if rel and rel in safe_set:
            untracked.append(rel)
    new_files = _heads(root, _safe_paths(root, untracked), reader) if untracked else ""

    committed = ""
    base_note = "no turn base recorded"
    base = str(base or "").strip()
    if base and _range_usable(runner, root, base):
        committed = _diff(runner, root, f"{base}..HEAD", safe)
        if committed:
            base_note = f"commits since turn start ({base[:12]}..HEAD)"
    elif base:
        base_note = "turn base is not an ancestor of HEAD; worktree only"

    sections: list[str] = []
    if committed or uncommitted or new_files:
        if committed:
            stat = _diff(runner, root, f"{base}..HEAD", safe, stat=True)
        else:
            stat = _diff(runner, root, "HEAD", safe, stat=True)
        if stat:
            sections.append(f"--- stat ({base_note}) ---\n{stat.rstrip()}")
        if committed:
            sections.append(f"--- committed during this turn ---\n{committed.rstrip()}")
        if uncommitted:
            sections.append(f"--- uncommitted ---\n{uncommitted.rstrip()}")
        if new_files:
            sections.append(f"--- new files ---\n{new_files.rstrip()}")
    elif base:
        # Nothing changed since the base: reviewing nothing is worse than
        # skipping, and the caller turns an empty result into "no review".
        return ""

    text = "\n\n".join(sections)
    limit = max(0, int(max_chars))
    if len(text) <= limit:
        return text
    # Stat first, then as much patch as the budget allows: a truncated patch
    # with no file list hides which files changed, and a file list with no
    # patch hides what changed. Keep both, clipped.
    first, _, rest = text.partition("\n\n")
    if first.startswith("--- stat"):
        note = "\n\n[diff truncated]"
        budget = limit - len(first) - len(note) - 2
        if budget > 0:
            return f"{first}\n\n{rest[:budget].rstrip()}{note}"
        keep = max(0, limit - len(note))
        return first[:keep].rstrip() + note
    return text[:limit]
