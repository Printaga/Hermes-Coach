"""Guidance files the coach carries into every call.

Global guidance lives at ``$HERMES_HOME/coach/guidance.md``; project guidance
is ``<cwd>/COACH.md``. Each file is capped at 4000 characters and the combined
block is wrapped in tags so the model can tell guidance from evidence.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["GUIDANCE_MAX_CHARS", "Guidance"]

GUIDANCE_MAX_CHARS = 4000
_GLOBAL_FILE = "guidance.md"
_PROJECT_FILE = "COACH.md"
_PROJECT_SCOPES = {"project", "local", "cwd"}


class Guidance:
    """Reads and writes the two guidance files."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, scope: str, cwd: str) -> Path:
        name = str(scope or "").strip().lower()
        if name in _PROJECT_SCOPES:
            return Path(cwd).expanduser() / _PROJECT_FILE
        return self.root / _GLOBAL_FILE

    def read(self, scope: str, cwd: str) -> str:
        """Return one guidance file, capped; a missing file is empty."""
        try:
            text = self._path(scope, cwd).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""
        return text[:GUIDANCE_MAX_CHARS]

    def write(self, scope: str, cwd: str, text: str) -> str:
        """Replace one guidance file, creating its directory if needed."""
        path = self._path(scope, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = str(text or "")
        path.write_text(body, encoding="utf-8")
        return f"saved {len(body)} chars to {path}"

    def system_block(self, cwd: str) -> str:
        """Return both files as one bounded, tagged block, or ``""``."""
        parts = [
            text.strip()
            for text in (self.read("global", cwd), self.read("project", cwd))
            if text.strip()
        ]
        if not parts:
            return ""
        body = "\n\n".join(parts)
        return f"<coach-guidance>\n{body}\n</coach-guidance>"
