"""Single worker thread for coach reviews.

One daemon thread per plugin process, one review in flight, and a coalescing
queue that keeps only the newest session. ``submit`` never blocks and never
waits for the host's turn; the hook path hands the work off and returns.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

__all__ = ["Worker"]


class Worker:
    """Runs at most one review at a time; newer requests replace queued ones."""

    def __init__(self, run: Callable[[str], None]) -> None:
        self._run = run
        self._condition = threading.Condition()
        self._pending: str | None = None
        self._running = False
        self._stopped = False
        self._thread = threading.Thread(
            target=self._loop, name="coach-worker", daemon=True
        )
        self._thread.start()

    def submit(self, session_id: str) -> bool:
        """Queue one session for review, replacing any waiting session."""
        session = str(session_id or "").strip()
        if not session:
            return False
        with self._condition:
            if self._stopped:
                return False
            self._pending = session
            self._condition.notify()
            return True

    def drain(self, timeout: float | None = None) -> bool:
        """Wait for pending work to finish; True when the worker is idle."""
        deadline = (
            None
            if timeout is None
            else time.monotonic() + max(0.0, float(timeout))
        )
        with self._condition:
            while self._running or self._pending is not None:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def stop(self) -> None:
        """Stop after the in-flight review, dropping anything queued."""
        with self._condition:
            self._stopped = True
            self._pending = None
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
                session = self._pending
                self._pending = None
                self._running = True
            try:
                self._run(session)
            except Exception:  # fail-open: a bad review never kills the worker
                pass
            finally:
                with self._condition:
                    self._running = False
                    self._condition.notify_all()
