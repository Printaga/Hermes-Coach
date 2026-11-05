"""One LLM call for the coach plugin, with deadline, usage and cost.

The client is deliberately fail-open: an unconfigured coach, a provider error
or a silent host fallback all come back as a :class:`CoachReply` with an
``error`` instead of an exception, so a bad turn never takes down the host.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from .findings import SCHEMA, Finding, parse_finding
from .settings import ModelChoice, Settings

__all__ = ["Coach", "CoachReply"]

_MODE_OVERRIDE_FIELDS: dict[str, str] = {
    "consult": "consult_model",
    "active": "active_model",
    "review": "review_model",
}

_STRUCTURED_INSTRUCTIONS = (
    "Assess the conversation and return one finding that matches the schema. "
    'Use severity "none" when there is nothing worth reporting.'
)


@dataclass(frozen=True)
class CoachReply:
    """The outcome of one coach call; ``error`` is empty on success."""

    text: str = ""
    finding: Finding | None = None
    provider: str = ""
    model: str = ""
    usage: Any = None
    cost_usd: float = 0.0
    error: str = ""


def _content_text(content: Any) -> str:
    """Flatten a message content value to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _split_messages(
    messages: Sequence[Mapping[str, Any]] | None,
) -> tuple[str, str]:
    """Split messages into a system prompt and a plain-text body."""
    system_parts: list[str] = []
    body_parts: list[str] = []
    for message in messages or []:
        if not isinstance(message, Mapping):
            continue
        text = _content_text(message.get("content"))
        if not text:
            continue
        if str(message.get("role") or "").strip().lower() == "system":
            system_parts.append(text)
        else:
            body_parts.append(text)
    return "\n\n".join(system_parts), "\n\n".join(body_parts)


def _differs(reported: str, requested: str) -> bool:
    """True when the host reported a different provider or model."""
    return bool(reported) and reported.strip().lower() != requested.strip().lower()


class Coach:
    """Calls the configured coach model and prices the result."""

    def __init__(
        self,
        llm: Any,
        settings: Settings,
        *,
        primary_model: str = "",
        primary_provider: str = "",
        cost_fn: Callable[[str, Any, str], float] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.primary_model = primary_model
        self.primary_provider = primary_provider
        self.cost_fn = cost_fn
        self.clock = clock

    def check(self, mode: str = "") -> str:
        """Return ``""`` when a call is allowed, else the blocking reason.

        ``mode`` is optional so a caller can ask about the shared model. A
        mode with its own explicit model override is exempt from the
        same-model guard: choosing it was deliberate.
        """
        if not self.settings.is_configured():
            return "unconfigured"
        choice: ModelChoice | None = (
            self.settings.model_for(mode) if mode else self.settings.coach_model
        )
        if choice is None:
            return "unconfigured"
        if not self.settings.skip_same_model:
            return ""
        override_field = _MODE_OVERRIDE_FIELDS.get(mode)
        if override_field and getattr(self.settings, override_field, None) is not None:
            return ""
        if self.settings.same_as_primary(
            choice, self.primary_model, self.primary_provider
        ):
            return "same_model"
        return ""

    def ask(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        mode: str,
        structured: bool = False,
        timeout: float = 45.0,
    ) -> CoachReply:
        """Run one call, mapping every failure mode to an error reply."""
        reason = self.check(mode)
        if reason:
            return CoachReply(error=reason)
        choice = self.settings.model_for(mode)
        if choice is None:
            return CoachReply(error="unconfigured")
        model, provider = choice.model, choice.provider

        try:
            if structured:
                system, body = _split_messages(messages)
                result = self.llm.complete_structured(
                    instructions=_STRUCTURED_INSTRUCTIONS,
                    input=[{"type": "text", "text": body}],
                    json_schema=SCHEMA,
                    system_prompt=system,
                    model=model,
                    provider=provider,
                    timeout=timeout,
                )
            else:
                result = self.llm.complete(
                    messages, model=model, provider=provider, timeout=timeout
                )
        except Exception as exc:  # fail-open by contract
            return CoachReply(error=f"provider_error: {type(exc).__name__}")

        text = str(getattr(result, "text", "") or "")
        actual_model = str(getattr(result, "model", "") or "")
        actual_provider = str(getattr(result, "provider", "") or "")
        usage = getattr(result, "usage", None)
        cost = self._price(model, usage, provider)

        if _differs(actual_model, model) or _differs(actual_provider, provider):
            return CoachReply(
                provider=actual_provider,
                model=actual_model,
                usage=usage,
                cost_usd=cost,
                error="fallback_mismatch",
            )

        return CoachReply(
            text=text,
            finding=parse_finding(getattr(result, "parsed", None), text),
            provider=actual_provider or provider,
            model=actual_model or model,
            usage=usage,
            cost_usd=cost,
        )

    def _price(self, model: str, usage: Any, provider: str) -> float:
        """Price one call, returning ``0.0`` when pricing is unavailable."""
        if self.cost_fn is not None:
            try:
                return float(self.cost_fn(model, usage, provider))
            except Exception:
                return 0.0
        if usage is None:
            return 0.0
        try:
            from agent.usage_pricing import estimate_usage_cost

            priced = estimate_usage_cost(model, usage, provider=provider)
            amount = getattr(priced, "amount_usd", priced)
            return float(amount or 0.0)
        except Exception:
            return 0.0
