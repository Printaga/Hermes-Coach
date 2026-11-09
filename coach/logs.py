"""The per-session JSONL event log.

One owner for the path, the append envelope (0700 dir, 0600 file, lock, trim)
and the two read shapes the command surface needs. The path identity is the
same hash the state store uses, so a session id cannot name another session's
log or escape the log directory.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state import safe_session_name

__all__ = [
    "append",
    "last_row",
    "log_name",
    "now_iso",
    "path_for",
    "read_tail",
    "trim",
]

_LOG_KEEP = 200
_LOG_TRIM_BYTES = 128 * 1024
_LOG_LOCK = threading.Lock()


def log_name(session_id: str) -> str:
    """A filesystem-safe, collision-free name for one session's log."""
    raw = str(session_id or "").strip()
    return safe_session_name(raw) if raw else "session"


def now_iso() -> str:
    """A wall-clock UTC timestamp for a log row."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def path_for(store_root: Path | str, session_id: str) -> Path:
    """``<HERMES_HOME>/coach/logs/<hashed-session>.jsonl``."""
    return Path(store_root).parent / "logs" / f"{log_name(session_id)}.jsonl"


def trim(path: Path, keep: int = _LOG_KEEP) -> None:
    """Keep the log bounded; drop the oldest lines when it grows."""
    try:
        if path.stat().st_size <= _LOG_TRIM_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= keep:
            return
        path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    except OSError:
        return


def append(store_root: Path | str, session_id: str, record: Any) -> None:
    """Append one JSON row, creating a 0700 dir and a 0600 file."""
    path = path_for(store_root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _LOG_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        trim(path)


def read_tail(path: Path, count: int) -> tuple[list[dict[str, Any]], int]:
    """The last ``count`` parsed rows, plus how many lines in that tail failed."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], 0
    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in lines[-max(1, count) :]:
        try:
            row = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(row, dict):
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


def last_row(path: Path) -> dict[str, Any] | None:
    """The newest parsed row, or None when the log is missing or unparseable."""
    rows, _ = read_tail(path, 1)
    return rows[-1] if rows else None
