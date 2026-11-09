"""Cross-process serialization for the session store.

``threading.RLock`` only orders threads inside one process. The desktop app and
a CLI session can both hold one state file and each pass a cap against the same
snapshot, so a load -> mutate -> save transaction needs an OS-level lock too.

:class:`StoreLock` combines both: the in-process RLock (re-entrant, so nesting
works) plus an advisory lock on ``<store root>/.lock``, taken only at depth
zero. It fails open: if the lock file cannot be opened or the platform offers
no file locking, the in-process lock still applies and a warning is logged once.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

__all__ = ["StoreLock"]

log = logging.getLogger(__name__)

# Windows attributes are absent from the POSIX type stubs pyright uses.
_MSVCRT_LOCKING = getattr(msvcrt, "locking", None) if msvcrt is not None else None
_MSVCRT_LOCK = getattr(msvcrt, "LK_LOCK", 2) if msvcrt is not None else 0
_MSVCRT_UNLOCK = getattr(msvcrt, "LK_UNLCK", 0) if msvcrt is not None else 0


class StoreLock:
    """A re-entrant in-process lock plus an OS advisory lock on one file."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None
        self._warned = False

    def __enter__(self) -> StoreLock:
        self._thread_lock.acquire()
        try:
            if self._depth == 0:
                self._acquire()
            self._depth += 1
        except BaseException:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self._depth -= 1
            if self._depth == 0:
                self._release()
        finally:
            self._thread_lock.release()

    def _acquire(self) -> None:
        try:
            self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            self._warn("coach: store lock unavailable: %s", exc)
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            elif _MSVCRT_LOCKING is not None:
                os.lseek(self._fd, 0, os.SEEK_SET)
                _MSVCRT_LOCKING(self._fd, _MSVCRT_LOCK, 1)
        except OSError as exc:
            self._warn("coach: store lock failed: %s", exc)
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif _MSVCRT_LOCKING is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                _MSVCRT_LOCKING(fd, _MSVCRT_UNLOCK, 1)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _warn(self, message: str, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            log.warning(message, exc)
