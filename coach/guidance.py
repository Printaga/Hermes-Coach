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
import os
import re
from pathlib import Path

__all__ = ["GUIDANCE_MAX_CHARS", "Guidance"]

GUIDANCE_MAX_CHARS = 4000
_GLOBAL_FILE = "guidance.md"
_PROJECT_FILE = "COACH.md"
_PROJECT_SCOPES = {"project", "local", "cwd"}
_GLOBAL_SCOPES = {"global", "all", ""}

# A repo can carry COACH.md, so block delimiters must be neutralized
# case-insensitively and with any inner whitespace: otherwise injected text
# can close the block early and turn guidance into free-standing instructions,
# or reopen a nested block the model treats as authoritative.
_CLOSE_TAG = re.compile(r"</\s*coach-guidance\s*>", re.IGNORECASE)
_OPEN_TAG = re.compile(r"<\s*coach-guidance(?:\s[^>]*)?>", re.IGNORECASE)


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
            if path.is_symlink():
                # A cloned repo can carry COACH.md as a symlink to any readable
                # file; reading it would put that file in every prompt.
                return ""
            # O_NOFOLLOW closes the swap between the check above and the open,
            # and the bounded read refuses to decode a huge file.
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
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
            except OSError:
                # A denied or broken guidance file degrades to no guidance;
                # read() already distinguishes missing files from real errors
                # for direct callers that need the distinction.
                continue
            if text.strip():
                parts.append(text.strip())
        if not parts:
            return ""
        body = "\n\n".join(parts)
        body = _OPEN_TAG.sub("", _CLOSE_TAG.sub("", body))
        return f"<coach-guidance>\n{body}\n</coach-guidance>"
