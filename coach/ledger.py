"""Bounded ledger compiler for the coach plugin.

A turn renders as a heading, the user's message verbatim, a truncated
assistant answer and tool cards. Assembly drops the oldest tool cards, then
old assistant text, then old turn headings until the ledger fits its
character ceiling; user messages are dropped only as an absolute last resort,
oldest first, so the newest context always survives.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .coerce import as_int
from .state import SessionState

__all__ = [
    "bound_turn","_bound_card", "append_turn", "build_messages", "estimate_tokens", "tool_card"]

_ASSISTANT_CHARS = 600
_USER_CHARS = 4000
_NAME_CHARS = 64
_ARG_CHARS = 256
_STATUS_CHARS = 64
_PREVIEW_WRITE_MAX = 2000
_CARDS_PER_TURN_MAX = 20
_MAX_TURNS = 60
# Host-supplied scalars: bound like every other written field, or a hostile or
# corrupt record smuggles an unbounded string past the documented write cap.
_TURN_ID_CHARS = 64
_CWD_CHARS = 512
_DIGEST_CHARS = 64
# Pre-turn guidance provenance: kept only when present so ordinary turn
# records stay exactly as before.
_PRE_GUIDANCE_CHARS = 500
_PRE_REASON_CHARS = 64


def _bound_card(card: Any) -> dict:
    """Truncate one tool card so raw payloads cannot persist unbounded."""
    if not isinstance(card, dict):
        return {"name": "tool", "arg": "", "status": "", "size": 0, "preview": ""}
    try:
        size = as_int(card.get("size"), 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "name": str(card.get("name") or "tool")[:_NAME_CHARS],
        "arg": str(card.get("arg") or "")[:_ARG_CHARS],
        "status": str(card.get("status") or "")[:_STATUS_CHARS],
        "size": max(0, size),
        "preview": str(card.get("preview") or "")[:_PREVIEW_WRITE_MAX],
    }


def bound_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with the ledger's write-time caps applied.

    Every write path goes through this. The plugin's own recorder used to copy
    the host payload verbatim, so the documented bound held only for the
    consult path and a single large turn could leave a 254 KB session file.
    """
    bounded = dict(turn)
    bounded["user"] = str(bounded.get("user") or "")[:_USER_CHARS]
    bounded["assistant"] = str(bounded.get("assistant") or "")[:_ASSISTANT_CHARS]
    tools = bounded.get("tools")
    if isinstance(tools, list):
        bounded["tools"] = [
            _bound_card(card) for card in tools[-_CARDS_PER_TURN_MAX:]
        ]
    else:
        bounded["tools"] = []
    # Only the known keys: an arbitrary payload key must not persist on disk.
    known = {
        "turn_id": str(bounded.get("turn_id") or "")[:_TURN_ID_CHARS],
        "user": bounded["user"],
        "assistant": bounded["assistant"],
        "tools": bounded["tools"],
        "cwd": str(bounded.get("cwd") or "")[:_CWD_CHARS],
        "digest": str(bounded.get("digest") or "")[:_DIGEST_CHARS],
    }
    if bounded.get("pre_guidance"):
        known["pre_guidance"] = str(bounded.get("pre_guidance") or "")[
            :_PRE_GUIDANCE_CHARS
        ]
    if bounded.get("pre_reason"):
        known["pre_reason"] = str(bounded.get("pre_reason") or "")[
            :_PRE_REASON_CHARS
        ]
    if bounded.get("pre_digest"):
        known["pre_digest"] = str(bounded.get("pre_digest") or "")[
            :_DIGEST_CHARS
        ]
    return known


def append_turn(
    state: SessionState,
    *,
    turn_id: str,
    user: str,
    assistant: str,
    tools: list[dict],
) -> None:
    """Record one completed turn on the session ledger.

    Text is bounded at write time so arbitrarily large model output or tool
    payloads cannot accumulate in memory or on disk: only rendering truncates
    is not enough, because the raw values persist in the session file.
    """
    state.ledger.append(
        bound_turn(
            {
                "turn_id": turn_id,
                "user": user,
                "assistant": assistant,
                "tools": tools,
            }
        )
    )
    if len(state.ledger) > _MAX_TURNS:
        del state.ledger[: len(state.ledger) - _MAX_TURNS]


def tool_card(
    name: str,
    arg: str,
    status: str,
    size: int,
    preview: str,
    *,
    max_chars: int,
) -> dict:
    """Build one compact tool record, truncating its preview."""
    limit = max(0, int(max_chars))
    try:
        size_int = max(0, as_int(size, 0))
    except (TypeError, ValueError):
        size_int = 0
    return {
        "name": str(name or "tool")[:_NAME_CHARS],
        "arg": str(arg or "")[:_ARG_CHARS],
        "status": str(status or "")[:_STATUS_CHARS],
        "size": size_int,
        "preview": str(preview or "")[:limit],
    }


def estimate_tokens(text: str) -> int:
    """Cheap ceiling estimate: roughly one token per four characters."""
    return (len(text) + 3) // 4


def _card_line(card: dict[str, Any], max_chars: int) -> str:
    name = str(card.get("name") or "tool")[:_NAME_CHARS]
    arg = str(card.get("arg") or "").strip()[:_ARG_CHARS]
    status = str(card.get("status") or "").strip()[:_STATUS_CHARS]
    try:
        size = max(0, as_int(card.get("size"), 0))
    except (TypeError, ValueError):
        size = 0
    preview = str(card.get("preview") or "")[: max(0, max_chars)]
    line = f"- {name}"
    if arg:
        line += f" {arg}"
    if status:
        line += f" [{status}]"
    line += f" {size}B"
    return f"{line}: {preview}" if preview else line


def _summary_line(count: int) -> str:
    return f"- {count} tool call{'s' if count != 1 else ''}"


def _turn_lines(
    ledger: Sequence[Any], *, keep_cards: int, preview_chars: int
) -> list[dict[str, Any]]:
    """Render the ledger as one line per field, tagged for the drop tiers."""
    lines: list[dict[str, Any]] = []
    total = len(ledger)
    for index, turn in enumerate(ledger):
        if not isinstance(turn, dict):
            continue
        assistant_text = str(turn.get("assistant") or "")
        tools = turn.get("tools")
        tool_list = (
            [card for card in tools if isinstance(card, dict)]
            if isinstance(tools, list)
            else []
        )
        lines.append(
            {"kind": "heading", "text": f"[turn {index + 1}]", "dropped": False}
        )
        lines.append(
            {
                "kind": "user",
                "text": f"user: {str(turn.get('user') or '')[:_USER_CHARS]}",
                "dropped": False,
            }
        )
        if assistant_text:
            lines.append(
                {
                    "kind": "assistant",
                    "text": f"assistant: {assistant_text[:_ASSISTANT_CHARS]}",
                    "dropped": False,
                }
            )
        if total - 1 - index < keep_cards:
            for card in tool_list:
                lines.append(
                    {
                        "kind": "card",
                        "text": _card_line(card, preview_chars),
                        "dropped": False,
                    }
                )
        elif tool_list:
            lines.append(
                {
                    "kind": "card",
                    "text": _summary_line(len(tool_list)),
                    "dropped": False,
                }
            )
    return lines


def _fit_to_budget(lines: list[dict[str, Any]], budget: int) -> str:
    """Drop by tier until the text fits, then cut the front on a line boundary.

    Tier order is the policy: cards go first, then assistant text, then turn
    headings; user history survives longest because the reviewer needs the
    request. The last resort keeps the newest user line and cuts the front.
    """

    def render() -> str:
        return "\n".join(line["text"] for line in lines if not line["dropped"])

    # Track the joined length incrementally: re-rendering per dropped line is
    # O(n^2) on a ledger with hundreds of lines. render() joins kept lines
    # with "\n", so n kept lines contribute n-1 separators, not n.
    kept = len(lines)
    total = sum(len(line["text"]) for line in lines) + max(0, kept - 1)

    def drop(line: dict[str, Any]) -> None:
        nonlocal kept, total
        line["dropped"] = True
        # Dropping from n>1 kept lines removes its text and one separator;
        # the last remaining line has no separator to remove.
        total -= len(line["text"]) + (1 if kept > 1 else 0)
        kept -= 1

    for kind in ("card", "assistant", "heading"):
        if total <= budget:
            break
        for line in lines:
            if total <= budget:
                break
            if line["kind"] == kind and not line["dropped"]:
                drop(line)
    ledger_text = render()

    kept_users = sum(
        1 for line in lines if line["kind"] == "user" and not line["dropped"]
    )
    if total > budget:
        for line in lines:
            if total <= budget or kept_users <= 1:
                break
            if line["kind"] == "user" and not line["dropped"]:
                drop(line)
                kept_users -= 1
        ledger_text = render()

    if len(ledger_text) <= budget:
        return ledger_text
    if budget <= 0:
        return ""
    start = max(0, len(ledger_text) - budget)
    cut = ledger_text.find("\n", start)
    clipped = ledger_text[cut + 1 :] if cut != -1 else ledger_text[-budget:]
    return clipped.lstrip()


def build_messages(
    *,
    system: str,
    guidance: str,
    state: SessionState,
    question: str | None,
    max_tokens: int,
    tool_result_chars: int,
    recent_turns: int,
    note_max_chars: int = 1000,
) -> list[dict[str, str]]:
    """Assemble the coach prompt from the bounded ledger."""
    preview_chars = max(0, int(tool_result_chars))
    keep_cards = max(0, as_int(recent_turns, 0))
    lines = _turn_lines(
        state.ledger, keep_cards=keep_cards, preview_chars=preview_chars
    )

    note_cap = max(0, int(note_max_chars))
    note_body = str(state.note or "")[:note_cap] if note_cap else ""
    note_text = (
        f"Session note (the coach's own interpretation):\n{note_body}"
        if note_body
        else ""
    )
    fixed = len(str(system or "")) + len(str(guidance or "")) + len(note_text)
    fixed += len(str(question or ""))
    budget = max(0, int(max_tokens) * 4 - fixed)
    ledger_text = _fit_to_budget(lines, budget)

    system_parts = [str(system or ""), str(guidance or "")]
    if note_text:
        system_parts.append(note_text)
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": "\n\n".join(part for part in system_parts if part),
        },
        {"role": "user", "content": ledger_text},
    ]
    if question:
        messages.append({"role": "user", "content": question})
    return messages
