"""User-visible card text for advice, decisions and holds.

Pure formatters: model text goes through :func:`coach.delivery.sanitize`, the
display caps live here, and nothing in this module touches state or the host.
"""

from __future__ import annotations

from typing import Any

from .delivery import sanitize
from .findings import Finding

__all__ = ["advice_card", "decision_card", "hold_message"]

# Display caps. These are presentation choices, not schema bounds; keep them
# together so retuning is one edit rather than a search for literals.
ADVICE_BODY_CHARS = 900
CARD_RATIONALE_CHARS = 400
HOLD_BODY_CHARS = 400
HOLD_EVIDENCE_CHARS = 200


def advice_card(text: str, intent: str, provider: str, model: str) -> str:
    """One user-visible card summarising a consult answer."""
    body = sanitize(text).strip()
    if len(body) > ADVICE_BODY_CHARS:
        body = body[:ADVICE_BODY_CHARS].rstrip() + " ..."
    who = f"{provider}/{model}" if provider or model else "coach"
    return "\n".join(
        part
        for part in (
            f"Coach advice ({intent}) · {who}",
            body,
            "Full text: /coach last · replay: coach_ask",
        )
        if part
    )


def decision_card(decision: Any, backend: str) -> str:
    """One user-visible card summarising a typed decision."""
    rationale = sanitize(decision.rationale).strip()
    if len(rationale) > CARD_RATIONALE_CHARS:
        rationale = rationale[:CARD_RATIONALE_CHARS].rstrip() + " ..."
    return "\n".join(
        part
        for part in (
            f"Coach decision · {backend} — {decision.decision} "
            f"({decision.confidence:.2f}, {decision.gate})",
            rationale,
        )
        if part
    )


def hold_message(finding: Finding) -> str:
    """One self-contained message for the held turn: what, why, evidence, fix."""
    title = sanitize(finding.title).strip().rstrip(".")
    body = sanitize(finding.body).strip()
    evidence = sanitize(finding.evidence).strip()
    fix = sanitize(finding.fix).strip().rstrip(".")
    lines = [f"Coach blocked completion: {title}."]
    if body:
        lines.append(body[:HOLD_BODY_CHARS])
    if evidence:
        lines.append(f"Evidence: {evidence[:HOLD_EVIDENCE_CHARS]}")
    if fix:
        lines.append(f"Suggested fix: {fix}")
    lines.append("Address it or explain why it does not apply, then finish.")
    return "\n".join(lines)
