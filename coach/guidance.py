"""Guidance files the coach carries into every call.

Global guidance lives at ``$HERMES_HOME/coach/guidance.md``; project guidance
is ``<cwd>/COACH.md``. Each file is read up to ``GUIDANCE_MAX_CHARS`` and the
combined block is wrapped in tags so the model can tell guidance from evidence.

The dashboard route is the only writer: it owns the symlink refusal, the
project allowlist and the ``O_NOFOLLOW`` open. This module used to carry a
second writer that none of it had, so a future caller could pick the weaker
one; it was deleted rather than kept in step.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path

from .scrub import strip_delimited

__all__ = ["GUIDANCE_MAX_CHARS", "Guidance"]

log = logging.getLogger(__name__)

GUIDANCE_MAX_CHARS = 4000
_GLOBAL_FILE = "guidance.md"
_PROJECT_FILE = "COACH.md"
_PROJECT_SCOPES = {"project", "local", "cwd"}
_GLOBAL_SCOPES = {"global", "all", ""}

# A repo can carry COACH.md, so block delimiters are neutralized before the
# block is assembled; the shared strip in coach.scrub handles split,
# attribute-bearing, zero-width and bare forms to a fixpoint.


class Guidance:
    """Reads the two guidance files."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, scope: str, cwd: str) -> Path:
        name = str(scope or "").strip().lower()
        if name in _PROJECT_SCOPES:
            return Path(cwd).expanduser() / _PROJECT_FILE
        if name in _GLOBAL_SCOPES:
            return self.root / _GLOBAL_FILE
        raise ValueError(f"unknown guidance scope: {scope!r}")

    def read(self, scope: str, cwd: str) -> str:
        """Return one guidance file, capped; a missing file is empty.

        Only a missing file (or a path that is not a file) reads as empty.
        Permission and I/O errors propagate so a denied or broken file is
        never silently treated as "no guidance".
        """
        path = self._path(scope, cwd)
        try:
            st = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(st.st_mode):
                # A cloned repo can carry COACH.md as a symlink (an O_NOFOLLOW
                # open already refuses that race) or as a special file like a
                # FIFO, whose open would block the single review thread
                # indefinitely; neither belongs in the prompt.
                return ""
            # O_NOFOLLOW closes the symlink swap between the check above and
            # the open; opening non-blocking and re-checking the type on the
            # descriptor closes the regular-file-to-FIFO swap that would
            # otherwise block this read forever. The bounded read refuses to
            # decode a huge file.
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            descriptor = os.open(path, flags)
            try:
                regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
            except OSError:
                os.close(descriptor)
                raise
            if not regular:
                os.close(descriptor)
                return ""
            set_blocking = getattr(os, "set_blocking", None)
            if set_blocking is not None and getattr(os, "O_NONBLOCK", 0):
                # Only where O_NONBLOCK exists (POSIX): the descriptor above
                # was opened non-blocking, so restore blocking reads. On
                # Windows the flag is 0 and set_blocking rejects the handle.
                try:
                    set_blocking(descriptor, True)
                except OSError:
                    os.close(descriptor)
                    raise
            with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(GUIDANCE_MAX_CHARS)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return ""
            if isinstance(
                exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError)
            ):
                return ""
            raise

    def system_block(self, cwd: str) -> str:
        """Return both files as one bounded, tagged block, or ``""``."""
        parts = []
        for scope in ("global", "project"):
            try:
                text = self.read(scope, cwd)
            except OSError as exc:
                # A denied or broken guidance file degrades to no guidance;
                # read() already distinguishes missing files from real errors
                # for direct callers that need the distinction. Log it so an
                # operator can tell this apart from "nothing configured".
                log.warning("coach: guidance file unreadable: %s", exc)
                continue
            if text.strip():
                parts.append(text.strip())
        if not parts:
            return ""
        body = strip_delimited("\n\n".join(parts), "coach-guidance")
        return f"<coach-guidance>\n{body}\n</coach-guidance>"
