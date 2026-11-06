"""One LLM call for the coach plugin, with deadline, usage and cost.

The client is deliberately fail-open: an unconfigured coach, a provider error
or a silent host fallback all come back as a :class:`CoachReply` with an
``error`` instead of an exception, so a bad turn never takes down the host.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from .findings import SCHEMA, Finding, parse_finding
from .settings import ModelChoice, Settings

__all__ = ["Coach", "CoachReply"]

_STRUCTURED_INSTRUCTIONS = (
    "Assess the conversation and return one finding that matches the schema. "
    'Use severity "none" when there is nothing worth reporting.'
)


@dataclass(frozen=True)
class CoachReply:
    """The outcome of one coach call; ``error`` is empty on success."""

    text: str = ""
    finding: Finding | None = None
    parsed: Any = None
    provider: str = ""
    model: str = ""
    usage: Any = None
    cost_usd: float = 0.0
    cost_estimated: bool = False
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
        role = str(message.get("role") or "user").strip().lower()
        if role == "system":
            system_parts.append(text)
        elif role == "user":
            body_parts.append(text)  # the ledger already labels user turns
        else:
            body_parts.append(f"{role}: {text}")
    return "\n\n".join(system_parts), "\n\n".join(body_parts)


def _differs(reported: str, requested: str) -> bool:
    """True when the host did not confirm the provider or model we asked for.

    An empty report is also a mismatch: Hermes always reports both fields, so
    a blank means the reply is unattributed, and silently trusting it would
    defeat the same-model guard. A false mismatch only discards one review
    (visible in the logs); a missed fallback would review the model's own
    work as if it were a second opinion.
    """
    if not requested.strip():
        return False
    return reported.strip().lower() != requested.strip().lower()


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
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.primary_model = primary_model
        self.primary_provider = primary_provider
        self.cost_fn = cost_fn

    def check(self, mode: str = "") -> str:
        """Return ``""`` when a call is allowed, else the blocking reason.

        ``mode`` is optional so a caller can ask about the shared model. The
        same-model guard applies to every mode, including one with its own
        override: a coach running the primary model is the blind spot this
        plugin exists to avoid. ``skip_same_model: false`` is the only escape.
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
        schema: Mapping[str, Any] | None = None,
        instructions: str = "",
        timeout: float = 45.0,
    ) -> CoachReply:
        """Run one call, mapping every failure mode to an error reply."""
        reason = self.check(mode)
        if reason:
            return CoachReply(error=reason)
        choice = self.settings.model_for(mode) if mode else self.settings.coach_model
        if choice is None:
            return CoachReply(error="unconfigured")
        model, provider = choice.model, choice.provider

        try:
            if structured:
                system, body = _split_messages(messages)
                if not body.strip():
                    # Nothing to review: fail open without a billable call.
                    return CoachReply(error="empty_prompt")
                result = self.llm.complete_structured(
                    instructions=instructions or _STRUCTURED_INSTRUCTIONS,
                    input=[{"type": "text", "text": body}],
                    json_schema=schema if schema is not None else SCHEMA,
                    system_prompt=system,
                    model=model,
                    provider=provider,
                    max_tokens=self.settings.max_reply_tokens,
                    timeout=timeout,
                )
            else:
                result = self.llm.complete(
                    messages,
                    model=model,
                    provider=provider,
                    max_tokens=self.settings.max_reply_tokens,
                    timeout=timeout,
                )
        except Exception as exc:  # fail-open by contract
            return CoachReply(error=f"provider_error: {type(exc).__name__}")

        text = str(getattr(result, "text", "") or "")
        parsed = getattr(result, "parsed", None)
        actual_model = str(getattr(result, "model", "") or "")
        actual_provider = str(getattr(result, "provider", "") or "")
        usage = getattr(result, "usage", None)
        # Price what served the call, not what was asked for: a host-side
        # fallback is billed at the fallback model's rate.
        cost, estimated = self._price(
            actual_model or model, usage, actual_provider or provider
        )

        if _differs(actual_model, model) or _differs(actual_provider, provider):
            return CoachReply(
                provider=actual_provider,
                model=actual_model,
                usage=usage,
                cost_usd=cost,
                cost_estimated=estimated,
                error="fallback_mismatch",
            )

        return CoachReply(
            text=text,
            finding=parse_finding(parsed, text),
            parsed=parsed,
            provider=actual_provider or provider,
            model=actual_model or model,
            usage=usage,
            cost_usd=cost,
            cost_estimated=estimated,
        )

    def _price(
        self, model: str, usage: Any, provider: str
    ) -> tuple[float, bool]:
        """Price one call; ``(cost, estimated)`` where estimated means guessed.

        The host's table does not know every provider (``opencode-go`` returns
        ``amount_usd=None``), and a None that becomes 0.0 makes the session
        budget decorative: every review is free and the stop never fires. An
        unpriced call is estimated from its token counts instead.
        """
        if self.cost_fn is not None:
            try:
                return max(0.0, float(self.cost_fn(model, usage, provider))), False
            except Exception:
                return 0.0, False
        if usage is None:
            return 0.0, False
        try:
            from agent.usage_pricing import estimate_usage_cost

            priced = estimate_usage_cost(model, usage, provider=provider)
            amount = getattr(priced, "amount_usd", priced)
            if amount is None:
                return self._estimate(usage), True
            return max(0.0, float(amount or 0.0)), False
        except Exception:
            # No host pricing module at all (or it raised): the token estimate
            # is still a better meter than a free call.
            return self._estimate(usage), True

    def _estimate(self, usage: Any) -> float:
        """Fallback price from token counts, at the configured blended rate."""
        rate = float(getattr(self.settings, "fallback_price_per_mtok", 1.0) or 0.0)
        if rate <= 0:
            return 0.0
        tokens = sum(
            max(0, int(getattr(usage, field, 0) or 0))
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        )
        return round(tokens * rate / 1_000_000, 9)
