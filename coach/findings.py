"""Structured findings for the coach plugin.

A finding is the coach's one actionable observation about a turn or a diff.
Parsing is strict: only a valid severity, title and body produce a finding, so
a malformed or unparseable reply becomes ``None`` instead of invented advice.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Finding",
    "SCHEMA",
    "SEVERITIES",
    "is_deliverable",
    "parse_finding",
]

SEVERITIES = ("none", "nit", "concern", "blocker")

_DELIVERABLE = ("nit", "concern", "blocker")
_TITLE_MAX = 80
_BODY_MAX = 600
_FENCED_JSON = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
)

SCHEMA: dict = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "title": {"type": "string", "maxLength": _TITLE_MAX},
        "body": {"type": "string", "maxLength": _BODY_MAX},
        "evidence": {"type": "string"},
        "fix": {"type": "string"},
    },
    "required": ["severity", "title", "body", "evidence", "fix"],
    "additionalProperties": False,
}


def _normalise(text: str) -> str:
    return " ".join(str(text).split()).lower()


@dataclass(frozen=True)
class Finding:
    """One review result: what, where, and how to fix it."""

    severity: str
    title: str
    body: str
    evidence: str
    fix: str

    def digest(self) -> str:
        """Stable identity for dedupe, ignoring whitespace and case."""
        payload = (
            f"{_normalise(self.severity)}|"
            f"{_normalise(self.title)}|"
            f"{_normalise(self.evidence)}"
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _from_mapping(payload: Mapping[str, Any]) -> Finding | None:
    severity = str(payload.get("severity") or "").strip().lower()
    if severity == "none":
        return None
    if severity not in _DELIVERABLE:
        return None
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not title or not body:
        return None
    return Finding(
        severity=severity,
        title=title[:_TITLE_MAX],
        body=body[:_BODY_MAX],
        evidence=str(payload.get("evidence") or "").strip(),
        fix=str(payload.get("fix") or "").strip(),
    )


def parse_finding(parsed: Any, text: str = "") -> Finding | None:
    """Return a validated finding from structured data or a fenced JSON reply."""
    if isinstance(parsed, Mapping):
        return _from_mapping(parsed)
    source = text or (parsed if isinstance(parsed, str) else "")
    for match in _FENCED_JSON.finditer(source):
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, Mapping):
            finding = _from_mapping(payload)
            if finding is not None:
                return finding
    return None


def is_deliverable(finding: Finding | None) -> bool:
    """True only for a parsed nit, concern or blocker."""
    return finding is not None and finding.severity in _DELIVERABLE
