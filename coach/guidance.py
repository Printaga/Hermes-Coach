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
_GLOBAL_SCOPES = {"global", "all", ""}


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
        parts = [
            text.strip()
            for text in (self.read("global", cwd), self.read("project", cwd))
            if text.strip()
        ]
        if not parts:
            return ""
        # A repo can carry COACH.md, so strip the closing tag: it must not close
        # the block early and turn guidance into free-standing instructions.
        body = "\n\n".join(parts).replace("</coach-guidance>", "")
        return f"<coach-guidance>\n{body}\n</coach-guidance>"
