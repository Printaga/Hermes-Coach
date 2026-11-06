"""Routing and formatting for coach findings.

A finding either becomes an ambient note the model sees on a later turn, a
corrective message that starts a turn now, or is dropped. Messaging surfaces
never inject: an injected turn there has no live agent to act on it.
"""

from __future__ import annotations

import re

from .findings import Finding, is_deliverable
from .settings import Settings

__all__ = ["format_ambient", "format_card", "format_corrective", "route"]

_MESSAGING_SURFACES = {
    "discord",
    "email",
    "imessage",
    "matrix",
    "signal",
    "slack",
    "sms",
    "teams",
    "telegram",
    "whatsapp",
}


def _capped(settings: Settings, holds_used: int) -> bool:
    cap = max(0, int(settings.delivery_max_corrective_per_request))
    return int(holds_used) >= cap


def route(
    finding: Finding | None,
    settings: Settings,
    *,
    surface: str,
    holds_used: int,
) -> str:
    """Choose ``ambient``, ``corrective`` or ``drop`` for one finding."""
    if not is_deliverable(finding):
        return "drop"
    if str(surface or "").strip().lower() in _MESSAGING_SURFACES:
        return "ambient"

    policy = settings.delivery_policy
    capped = _capped(settings, holds_used)
    if policy == "ambient":
        return "ambient"
    if policy == "corrective":
        return "ambient" if capped else "corrective"
    # The default severity policy: only blockers may start a turn.
    if finding.severity == "blocker" and not capped:
        return "corrective"
    return "ambient"


def _attr(value: object) -> str:
    return (
        str(value or "")
        .strip()
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


_NOTE_CLOSE = re.compile(r"</\s*coach-note\s*>", re.IGNORECASE)
_NOTE_OPEN = re.compile(r"<\s*coach-note\b[^>]*>", re.IGNORECASE)


def _text(value: object) -> str:
    """Neutralize note block delimiters in model-supplied text.

    Finding text rides back into model context inside ``<coach-note>``, so both
    the closing and opening forms are stripped case-insensitively (allowing
    inner whitespace) before the note is assembled.
    """
    text = str(value or "")
    text = _NOTE_CLOSE.sub("", text)
    return _NOTE_OPEN.sub("", text)


def sanitize(value: object) -> str:
    """Public form of the block-delimiter strip, for messages outside a note."""
    return _text(value)


def _lines(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def format_ambient(finding: Finding, turn_id: str) -> str:
    """Format the once-seen context block attached before a later turn."""
    evidence = f"Evidence: {finding.evidence}" if finding.evidence else ""
    fix = f"Suggested fix: {finding.fix}" if finding.fix else ""
    return _lines(
        (
            f'<coach-note turn="{_attr(turn_id)}" '
            f'severity="{_attr(finding.severity)}" source="coach">'
        ),
        f"Coach review found a {finding.severity}: {_text(finding.title)}",
        _text(finding.body),
        _text(evidence),
        _text(fix),
        "Weigh this against the user's current request; the user's instruction wins.",
        "</coach-note>",
    )


def format_corrective(finding: Finding) -> str:
    """Format a message that starts a live turn for the agent to act on."""
    evidence = f"Evidence: {finding.evidence}" if finding.evidence else ""
    fix = f"Suggested fix: {finding.fix}" if finding.fix else ""
    return _lines(
        f"Coach review found a {finding.severity}: {_text(finding.title)}",
        _text(finding.body),
        _text(evidence),
        _text(fix),
        (
            "Address this before continuing. This note came from an independent "
            "review, not from the user."
        ),
    )


def format_card(finding: Finding) -> str:
    """Format the short, user-visible card."""
    evidence = f"Evidence: {_text(finding.evidence)}" if finding.evidence else ""
    fix = f"Fix: {_text(finding.fix)}" if finding.fix else ""
    return _lines(
        f"Coach · {finding.severity} — {_text(finding.title)}",
        _text(finding.body),
        evidence,
        fix,
        "Logged: /coach log · Send to chat: /coach send",
    )
