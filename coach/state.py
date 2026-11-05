"""Per-session state for the coach plugin.

One JSON file per session under the root the caller supplies
(``$HERMES_HOME/coach/sessions`` in the plugin). Writes are atomic and
private, pruning keeps the newest sessions, and a missing or corrupt file
always yields a fresh state instead of an exception.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["SessionState", "Store"]

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class SessionState:
    """Everything the coach remembers about one session."""

    session_id: str
    ledger: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    pending: list[dict[str, Any]] = field(default_factory=list)
    note_seq: int = 0
    holds: int = 0
    spend_usd: float = 0.0
    calls: dict[str, int] = field(default_factory=dict)
    last_error: str = ""
    updated_at: float = field(default_factory=time.time)


def _safe_name(session_id: str) -> str:
    """Map a session id to one filename component on every platform."""
    name = _UNSAFE_NAME.sub("_", str(session_id)).strip(".")
    return name or "session"


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else 0
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _calls(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _as_int(count) for key, count in value.items()}


class Store:
    """Session state on disk: atomic writes, coercion, pruning."""

    def __init__(self, root: Path, max_sessions: int = 20) -> None:
        self.root = Path(root)
        self.max_sessions = max_sessions

    def _path(self, session_id: str) -> Path:
        return self.root / f"{_safe_name(session_id)}.json"

    def _state_paths(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return [path for path in self.root.glob("*.json") if path.is_file()]

    def load(self, session_id: str) -> SessionState:
        """Return the stored state, or a fresh one; never raises."""
        try:
            with self._path(session_id).open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return SessionState(session_id=session_id)
        except (OSError, ValueError, TypeError):
            return SessionState(session_id=session_id, last_error="state reset")
        if not isinstance(data, dict):
            return SessionState(session_id=session_id, last_error="state reset")
        return SessionState(
            session_id=session_id,
            ledger=_dict_items(data.get("ledger")),
            note=_as_str(data.get("note")),
            pending=_dict_items(data.get("pending")),
            note_seq=_as_int(data.get("note_seq")),
            holds=_as_int(data.get("holds")),
            spend_usd=_as_float(data.get("spend_usd")),
            calls=_calls(data.get("calls")),
            last_error=_as_str(data.get("last_error")),
            updated_at=_as_float(data.get("updated_at"), time.time()),
        )

    def save(self, state: SessionState) -> None:
        """Write the state atomically as a private file."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(state.session_id)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.root, prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, ensure_ascii=False)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def prune(self) -> None:
        """Delete the oldest sessions beyond the cap."""
        entries = [
            (self.load(path.stem).updated_at, path.name, path)
            for path in self._state_paths()
        ]
        overflow = len(entries) - self.max_sessions
        if overflow <= 0:
            return
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        for _, _, path in entries[:overflow]:
            path.unlink(missing_ok=True)

    def forget(self, session_id: str) -> None:
        """Remove one session file, ignoring an already absent one."""
        try:
            self._path(session_id).unlink()
        except FileNotFoundError:
            pass

    def spend_total(self) -> float:
        """Sum recorded spend across every session on disk."""
        return sum(self.load(path.stem).spend_usd for path in self._state_paths())

    def sessions_on_disk(self) -> list[str]:
        """Session names with a state file, oldest-independent stable order."""
        return sorted(path.stem for path in self._state_paths())
