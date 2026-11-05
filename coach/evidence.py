"""Bounded diff evidence for the Review gate.

``collect`` asks git for the change set under the supplied paths and falls
back to reading the files directly when git is unavailable or fails. Paths are
project-relative: absolute paths and any ``..`` component are refused so the
hook cannot read outside the working directory.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

__all__ = ["collect"]

_GIT_TIMEOUT = 5


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _safe_paths(root: Path, paths: Sequence[str]) -> list[str]:
    base = root.resolve()
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
            except (OSError, ValueError):
                continue
            continue
        if ".." in candidate.parts:
            continue
        try:
            (base / candidate).resolve().relative_to(base)
        except (OSError, ValueError):
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


def _heads(
    root: Path,
    paths: Sequence[str],
    reader: Callable[[Path], str | None],
) -> str:
    parts: list[str] = []
    for rel in paths:
        body = reader(root / rel)
        if body is None:
            continue
        parts.append(f"--- {rel} (new file) ---\n{body}")
    return "\n".join(parts)


def collect(
    cwd: str,
    paths: Sequence[str],
    *,
    max_chars: int = 8000,
    run: Callable[..., object] | None = None,
    read: Callable[[Path], str | None] | None = None,
) -> str:
    """Return the changed-file evidence for ``paths`` under ``cwd``."""
    root = Path(cwd)
    safe = _safe_paths(root, paths)
    if not safe:
        return ""
    runner = run or subprocess.run
    reader = read or _read_text

    # -z keeps paths unquoted and unescaped (no C-style octal for non-ASCII).
    status = _git(
        runner,
        root,
        ["git", "-c", "core.quotePath=false", "status", "--porcelain", "-z", "--", *safe],
    )
    diff = _git(runner, root, ["git", "diff", "HEAD", "--", *safe])
    if (
        status is None
        or status.returncode != 0
        or diff is None
        or diff.returncode != 0
    ):
        text = _heads(root, safe, reader)
    else:
        text = _stdout(diff)
        untracked: list[str] = []
        for entry in _stdout(status).split("\0"):
            if not entry.startswith("?? "):
                continue
            rel = entry[3:]
            if rel and rel in safe:
                untracked.append(rel)
        if untracked:
            extra = _heads(root, _safe_paths(root, untracked), reader)
            if extra:
                text = f"{text}\n{extra}" if text else extra

    return text[: max(0, int(max_chars))]
