"""Guidance files the coach carries into every call.

Global guidance lives at ``$HERMES_HOME/coach/guidance.md``; project guidance
is ``<cwd>/COACH.md``. Each file is capped at 4000 characters and the combined
block is wrapped in tags so the model can tell guidance from evidence.
"""

from __future__ import annotations

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
    """Reads and writes the two guidance files."""

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
        try:
            text = self._path(scope, cwd).read_text(
                encoding="utf-8", errors="replace"
            )
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return ""
        return text[:GUIDANCE_MAX_CHARS]

    def write(self, scope: str, cwd: str, text: str) -> str:
        """Replace one guidance file, creating its directory if needed."""
        path = self._path(scope, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = str(text or "")
        capped = body[:GUIDANCE_MAX_CHARS]
        path.write_text(capped, encoding="utf-8")
        if len(capped) < len(body):
            return (
                f"saved {len(capped)} of {len(body)} chars to {path} "
                f"(limit {GUIDANCE_MAX_CHARS})"
            )
        return f"saved {len(capped)} chars to {path}"

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
