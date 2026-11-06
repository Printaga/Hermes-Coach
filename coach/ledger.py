"""Bounded ledger compiler for the coach plugin.

A turn renders as a heading, the user's message verbatim, a truncated
assistant answer and tool cards. Assembly drops the oldest tool cards, then
old assistant text, then old turn headings until the ledger fits its
character ceiling; user messages are dropped only as an absolute last resort,
oldest first, so the newest context always survives.
"""

from __future__ import annotations

from typing import Any

from .state import SessionState

__all__ = ["append_turn", "build_messages", "estimate_tokens", "tool_card"]

_ASSISTANT_CHARS = 600
_USER_CHARS = 4000
_NAME_CHARS = 64
_ARG_CHARS = 256
_STATUS_CHARS = 64
_MAX_TURNS = 60


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
        {
            "turn_id": turn_id,
            "user": str(user or "")[:_USER_CHARS],
            "assistant": str(assistant or "")[:_ASSISTANT_CHARS],
            "tools": [
                dict(card) if isinstance(card, dict) else card for card in tools
            ],
        }
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
    return {
        "name": name,
        "arg": arg,
        "status": status,
        "size": size,
        "preview": str(preview)[:limit],
    }


def estimate_tokens(text: str) -> int:
    """Cheap ceiling estimate: roughly one token per four characters."""
    return (len(text) + 3) // 4


def _card_line(card: dict[str, Any], max_chars: int) -> str:
    name = str(card.get("name") or "tool")[:_NAME_CHARS]
    arg = str(card.get("arg") or "").strip()[:_ARG_CHARS]
    status = str(card.get("status") or "").strip()[:_STATUS_CHARS]
    size = card.get("size", 0)
    preview = str(card.get("preview") or "")[:max_chars]
    line = f"- {name}"
    if arg:
        line += f" {arg}"
    if status:
        line += f" [{status}]"
    line += f" {size}B"
    return f"{line}: {preview}" if preview else line


def _summary_line(count: int) -> str:
    return f"- {count} tool call{'s' if count != 1 else ''}"


def build_messages(
    *,
    system: str,
    guidance: str,
    state: SessionState,
    question: str | None,
    max_tokens: int,
    tool_result_chars: int,
    recent_turns: int,
) -> list[dict[str, str]]:
    """Assemble the coach prompt from the bounded ledger."""
    lines: list[dict[str, Any]] = []
    preview_chars = max(0, int(tool_result_chars))
    keep_cards = max(0, int(recent_turns))
    total = len(state.ledger)

    for index, turn in enumerate(state.ledger):
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
                "text": f"user: {turn.get('user') or ''}",
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

    note_text = (
        f"Session note (the coach's own interpretation):\n{state.note}"
        if state.note
        else ""
    )
    fixed = len(str(system or "")) + len(str(guidance or "")) + len(note_text)
    fixed += len(str(question or ""))
    budget = max(0, int(max_tokens) * 4 - fixed)

    def render() -> str:
        return "\n".join(line["text"] for line in lines if not line["dropped"])

    ledger_text = render()
    for kind in ("card", "assistant", "heading"):
        if len(ledger_text) <= budget:
            break
        for line in lines:
            if len(ledger_text) <= budget:
                break
            if line["kind"] == kind and not line["dropped"]:
                line["dropped"] = True
                ledger_text = render()

    if len(ledger_text) > budget:
        # Last resort: the user history alone is oversized. Drop the oldest
        # user lines first so the newest context survives, but always keep at
        # least one user line; only then keep the tail, cut on a line boundary.
        def _kept_user_lines() -> list[Any]:
            return [
                line
                for line in lines
                if line["kind"] == "user" and not line["dropped"]
            ]

        for line in lines:
            if len(ledger_text) <= budget or len(_kept_user_lines()) <= 1:
                break
            if line["kind"] == "user" and not line["dropped"]:
                line["dropped"] = True
                ledger_text = render()
    if len(ledger_text) > budget:
        start = max(0, len(ledger_text) - budget)
        cut = ledger_text.find("\n", start)
        ledger_text = ledger_text[cut + 1 :] if cut != -1 else ledger_text[-budget:]
        ledger_text = ledger_text.lstrip()

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
