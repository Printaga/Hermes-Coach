"""Structured findings for the coach plugin.

A finding is the coach's one actionable observation about a turn or a diff.
Parsing is strict: only a valid severity, title and body produce a finding, so
a malformed or unparseable reply becomes ``None`` instead of invented advice.
Over-long optional fields are truncated to the SCHEMA limits rather than
rejected: the alternative is dropping an otherwise valid finding over a long
evidence excerpt.
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

SEVERITIES = ("none", "nit", "concern", "unverifiable", "blocker")

_DELIVERABLE = ("nit", "concern", "unverifiable", "blocker")
_TITLE_MAX = 80
_BODY_MAX = 600
_EVIDENCE_MAX = 1000
_FIX_MAX = 1000
_FENCED_BLOCK = re.compile(
    r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE
)

SCHEMA: dict = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "title": {"type": "string", "maxLength": _TITLE_MAX},
        "body": {"type": "string", "maxLength": _BODY_MAX},
        "evidence": {"type": "string", "maxLength": _EVIDENCE_MAX},
        "fix": {"type": "string", "maxLength": _FIX_MAX},
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
        """Stable identity for dedupe, ignoring whitespace and case.

        Body and fix are part of the identity: two findings that share a
        title and an evidence excerpt but give materially different advice
        must not collapse into one.
        """
        payload = (
            f"{_normalise(self.severity)}|"
            f"{_normalise(self.title)}|"
            f"{_normalise(self.body)}|"
            f"{_normalise(self.evidence)}|"
            f"{_normalise(self.fix)}"
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
        evidence=str(payload.get("evidence") or "").strip()[:_EVIDENCE_MAX],
        fix=str(payload.get("fix") or "").strip()[:_FIX_MAX],
    )


def _json_objects(text: str):
    """Yield every balanced ``{...}`` span, ignoring braces inside strings.

    The old non-greedy ``\\{.*?\\}`` stopped at the first closing brace — one
    inside a string literal — so a valid reply like
    ``{"body": "the check uses } here"}`` became unparseable and the finding
    was silently dropped.
    """
    start = None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start : index + 1]
                start = None


def parse_finding(parsed: Any, text: str = "") -> Finding | None:
    """Return a validated finding from structured data or a fenced JSON block."""
    if isinstance(parsed, Mapping):
        return _from_mapping(parsed)
    source = text or (parsed if isinstance(parsed, str) else "")
    for block in _FENCED_BLOCK.finditer(source):
        chunk = block.group(1).strip()
        payloads: list[Any] = []
        try:
            payloads.append(json.loads(chunk))
        except (TypeError, ValueError):
            for candidate in _json_objects(chunk):
                try:
                    payloads.append(json.loads(candidate))
                except (TypeError, ValueError):
                    continue
        for payload in payloads:
            if isinstance(payload, Mapping):
                finding = _from_mapping(payload)
                if finding is not None:
                    return finding
    return None


def is_deliverable(finding: Finding | None) -> bool:
    """True only for a parsed nit, concern, unverifiable or blocker."""
    return finding is not None and finding.severity in _DELIVERABLE
