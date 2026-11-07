"""Per-session state for the coach plugin.

One JSON file per session under the root the caller supplies
(``$HERMES_HOME/coach/sessions`` in the plugin). Writes are atomic and
private, pruning keeps the newest sessions, and a missing or corrupt file
always yields a fresh state instead of an exception.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .coerce import as_finite_float, as_int, as_str

__all__ = ["SessionState", "Store"]

log = logging.getLogger(__name__)

_MAX_NAME_CHARS = 80


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
    last_user_message: str = ""
    last_primary_model: str = ""
    last_advice: dict[str, Any] = field(default_factory=dict)
    advice: list[dict[str, Any]] = field(default_factory=list)
    last_decision: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


def _safe_name(session_id: str) -> str:
    """Map a session id to one collision-free filename component.

    Percent-encoding keeps the id reversible; the hash suffix stops two long
    ids from truncating to the same file (and stops case-insensitive
    filesystems from folding ``A`` and ``a`` together).
    """
    raw = str(session_id)
    digest = hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    quoted = urllib.parse.quote(raw, safe="")
    if len(quoted) > _MAX_NAME_CHARS:
        quoted = quoted[:_MAX_NAME_CHARS]
    return f"{quoted}-{digest}" if quoted else f"session-{digest}"


def _as_str(value: Any) -> str:
    return as_str(value)


def _as_int(value: Any) -> int:
    return as_int(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    return as_finite_float(value, default)


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
        self._saves = 0

    def _path(self, session_id: str) -> Path:
        return self.root / f"{_safe_name(session_id)}.json"

    def _state_paths(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return [path for path in self.root.glob("*.json") if path.is_file()]

    def _quarantine(self, path: Path) -> None:
        """Move a corrupt state file aside so its contents survive diagnosis.

        A reset (fresh) state silently drops spend, hold counters and the
        ledger; keeping the unreadable file under a distinct name makes the
        cause diagnosable instead of destroyed on the next save.
        """
        try:
            path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass

    def _read(self, path: Path, fallback_id: str) -> SessionState:
        """Parse one file; a corrupt or missing one is a fresh state.

        Corrupt content (bad JSON, wrong shape) resets to a fresh state that
        says so in ``last_error``; the unreadable file is renamed aside first
        so the damage is diagnosable rather than silently overwritten by the
        next save. Read failures that are not corruption (permissions, I/O)
        also yield a fresh state — the store is fail-open by contract — but
        ``last_error`` names the real cause so /coach status shows it
        instead of silently discarding history.
        """
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return SessionState(session_id=fallback_id)
        except (ValueError, TypeError, RecursionError):
            # json raises ValueError (incl. UnicodeDecodeError) on bad bytes;
            # TypeError/RecursionError cover hostile shapes.
            self._quarantine(path)
            return SessionState(
                session_id=fallback_id, last_error="state reset", updated_at=0.0
            )
        except OSError as exc:
            return SessionState(
                session_id=fallback_id,
                last_error=f"state unreadable: {type(exc).__name__}",
                updated_at=0.0,
            )
        if not isinstance(data, dict):
            self._quarantine(path)
            return SessionState(
                session_id=fallback_id, last_error="state reset", updated_at=0.0
            )
        return SessionState(
            session_id=_as_str(data.get("session_id")) or fallback_id,
            ledger=_dict_items(data.get("ledger")),
            note=_as_str(data.get("note"))[:5000],
            pending=_dict_items(data.get("pending")),
            note_seq=_as_int(data.get("note_seq")),
            holds=_as_int(data.get("holds")),
            spend_usd=_as_float(data.get("spend_usd")),
            calls=_calls(data.get("calls")),
            last_error=_as_str(data.get("last_error")),
            last_user_message=_as_str(data.get("last_user_message")),
            last_primary_model=_as_str(data.get("last_primary_model")),
            last_advice=(
                dict(data["last_advice"])
                if isinstance(data.get("last_advice"), Mapping)
                else {}
            ),
            advice=_dict_items(data.get("advice")),
            last_decision=(
                dict(data["last_decision"])
                if isinstance(data.get("last_decision"), Mapping)
                else {}
            ),
            updated_at=_as_float(data.get("updated_at"), 0.0),
        )

    def has(self, session_id: str) -> bool:
        """True when this session already has a state file."""
        try:
            return self._path(session_id).is_file()
        except OSError:
            return False

    def load(self, session_id: str) -> SessionState:
        """Return the stored state, or a fresh one; never raises."""
        return self._read(self._path(session_id), session_id)

    def save(self, state: SessionState) -> None:
        """Write the state atomically as a private file."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)  # mode= applies on creation only
        except OSError as exc:
            # The privacy guarantee is only as good as the directory mode; a
            # silent failure would pretend otherwise.
            log.warning("coach store root not private: %s: %s", self.root, exc)
        state.updated_at = time.time()
        # A non-finite spend would be written as Infinity and read back as 0.0,
        # resetting the budget stop. Clamp before serialising.
        state.spend_usd = _as_float(state.spend_usd, 0.0)
        path = self._path(state.session_id)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.root, prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, ensure_ascii=False, allow_nan=False)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                # Cleanup must never mask the exception that caused it.
                pass
            raise
        # The commit landed: everything below is best-effort, so a failure here
        # must not report the write as failed.
        self._saves += 1
        if self._saves % 25 == 0:
            try:
                # Sweep on the tick even under the cap: this is also where
                # crash-leftover temp files are removed.
                self.prune()
            except OSError as exc:
                # A persistent prune failure silently un-caps the directory.
                log.warning("coach store prune failed: %s", exc)

    def prune(self) -> None:
        """Delete the oldest sessions beyond the cap, ordered by file mtime."""
        entries = []
        for path in self._state_paths():
            try:
                stamp = path.stat().st_mtime
            except OSError:
                stamp = 0.0
            entries.append((stamp, path.name, path))
        overflow = len(entries) - self.max_sessions
        if overflow <= 0:
            self._sweep_tmp_files()
            return
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        for _, _, path in entries[:overflow]:
            path.unlink(missing_ok=True)
        self._sweep_tmp_files()

    def _sweep_tmp_files(self, older_than_s: float = 3600.0) -> None:
        """Remove stale dot-tmp files left by a crash between mkstemp and replace.

        They end in ``.tmp``, so the ``*.json`` glob and the session cap never
        see them; without this they accumulate indefinitely.
        """
        if not self.root.is_dir():
            return
        now = time.time()
        for tmp in self.root.glob(".*.tmp"):
            try:
                if not tmp.is_file():
                    continue
                if now - tmp.stat().st_mtime < older_than_s:
                    continue
                tmp.unlink(missing_ok=True)
            except OSError:
                continue

    def forget(self, session_id: str) -> None:
        """Remove one session file, ignoring an already absent one."""
        try:
            self._path(session_id).unlink()
        except FileNotFoundError:
            pass

    def spend_total(self) -> float:
        """Sum recorded spend across every readable session on disk."""
        return sum(
            state.spend_usd
            for state in (self._read_quiet(path) for path in self._state_paths())
            if state is not None
        )

    def sessions_by_recency(self) -> list[str]:
        """Session ids ordered by file mtime, newest first."""
        rows = []
        for path in self._state_paths():
            try:
                rows.append((path.stat().st_mtime, path))
            except OSError:
                continue
        rows.sort(key=lambda item: item[0], reverse=True)
        ids = []
        for _, path in rows:
            state = self._read_quiet(path)
            if state is not None:
                ids.append(state.session_id)
        return ids

    def states_on_disk(self) -> list[SessionState]:
        """Read every session file once, skipping unreadable ones."""
        states: list[SessionState] = []
        for path in self._state_paths():
            state = self._read_quiet(path)
            if state is not None:
                states.append(state)
        return states

    def sessions_on_disk(self) -> list[str]:
        """Original session ids with a readable state file, in a stable order."""
        return sorted(
            state.session_id
            for state in (self._read_quiet(path) for path in self._state_paths())
            if state is not None
        )

    def _read_quiet(self, path: Path) -> SessionState | None:
        """Read one file for aggregation; unreadable files are skipped.

        ``_read`` falls back to a fresh state whose ``session_id`` is the
        caller's fallback (here the encoded filename), so aggregations must
        not use it: a corrupt file would surface the encoded filename as a
        session id. Reading the stored id directly keeps the listings honest.
        """
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError, RecursionError):
            return None
        if not isinstance(data, dict):
            return None
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        return self._read(path, session_id)
