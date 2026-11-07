"""Settings for the coach plugin.

``DEFAULTS`` is the source of truth for values. ``plugin.yaml`` declares the
plugin's tools and hooks only — it carries no settings mirror.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .coerce import as_bool as as_coerce_bool
from .coerce import as_finite_float, as_int

__all__ = ["DEFAULTS", "ModelChoice", "Settings", "load_settings"]


@dataclass(frozen=True)
class ModelChoice:
    """The provider and model one mode should call.

    ``as_dict`` is the single wire shape the host, page and manifest share.
    """

    provider: str
    model: str

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}


DEFAULTS: dict[str, Any] = {
    # Models
    "coach_model": None,
    "reasoning_effort": "",
    "active_model": None,
    "active_reasoning": "",
    "review_model": None,
    "review_reasoning": "",
    "consult_model": None,
    "consult_reasoning": "",
    "max_reply_tokens": 400,
    # Modes
    "active_enabled": False,
    "active_cadence": "tool_turns_only",
    "active_every_n": 3,
    "review_enabled": True,
    "review_authority": "hold_once",
    "review_max_holds": 1,
    # How many characters of changed-file patch the completion review may see.
    # 8 000 was too small for multi-file turns: the review then reported the
    # truncation as "unverifiable" instead of judging the work. 128 000 covers
    # every observed commit in this repo's history except one 141k refactor.
    "review_evidence_max_chars": 128000,
    "decide_enabled": False,
    "decide_backend": "jev-openrouter",
    "decide_model": "typesafe/jev-1.14",
    "decide_min_confidence": 0.60,
    "decide_max_calls_per_turn": 3,
    "consult_max_calls_per_turn": 4,
    "decide_typesafe_api_key": "",
    # Delivery
    "delivery_policy": "severity",
    "delivery_max_corrective_per_request": 1,
    "delivery_show_cards": True,
    "delivery_note_ttl_turns": 2,
    # Budget
    "budget_session_usd": 1.00,
    "fallback_price_per_mtok": 1.0,
    "budget_on_exceed": "soft",
    # Guidance
    "guidance_enabled": True,
    # Advanced
    "worker_timeout_s": 45,
    "ledger_max_tool_result_chars": 400,
    "ledger_max_recent_turns": 3,
    "ledger_note_max_chars": 1000,
    "ledger_max_tokens": 3500,
    "skip_same_model": True,
    "allow_in_subagents": False,
    "debug_log_decisions": False,
}

REASONING_LEVELS = (
    "",
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
_REASONING_FIELDS = {
    "coach": "reasoning_effort",
    "active": "active_reasoning",
    "review": "review_reasoning",
    "consult": "consult_reasoning",
}

_CHOICES: dict[str, tuple[str, ...]] = {
    "active_cadence": ("tool_turns_only", "every_turn", "every_n"),
    "review_authority": ("hold_once", "report"),
    "decide_backend": ("jev-openrouter", "jev-direct", "chat-model"),
    "delivery_policy": ("severity", "ambient", "corrective"),
    "budget_on_exceed": ("warn", "soft", "hard"),
    "reasoning_effort": REASONING_LEVELS,
    "active_reasoning": REASONING_LEVELS,
    "review_reasoning": REASONING_LEVELS,
    "consult_reasoning": REASONING_LEVELS,
}

_MODE_MODEL_FIELDS: dict[str, str] = {
    "consult": "consult_model",
    "active": "active_model",
    "review": "review_model",
}


def _as_bool(value: Any, default: bool) -> bool:
    return as_coerce_bool(value, default)


def _as_int(value: Any, default: int) -> int:
    return as_int(value, default)


def _as_float(value: Any, default: float) -> float:
    return as_finite_float(value, default)


def _as_text(value: Any, default: str) -> str:
    if not isinstance(value, str):
        return default
    return value.strip() or default


def _as_choice(value: Any, key: str, default: str) -> str:
    return value if value in _CHOICES[key] else default


def _as_confidence(value: Any) -> float:
    number = _as_float(value, DEFAULTS["decide_min_confidence"])
    return min(1.0, max(0.0, number))


def _as_model(value: Any) -> ModelChoice | None:
    if isinstance(value, ModelChoice):
        return value
    if not isinstance(value, Mapping):
        return None
    raw_provider = value.get("provider")
    raw_model = value.get("model")
    if not isinstance(raw_provider, str) or not isinstance(raw_model, str):
        return None
    provider = raw_provider.strip()
    model = raw_model.strip()
    if not provider or not model:
        return None
    return ModelChoice(provider=provider, model=model)


@dataclass(frozen=True)
class Settings:
    """Validated, immutable settings for one coach turn."""

    # Models
    coach_model: ModelChoice | None
    reasoning_effort: str
    active_model: ModelChoice | None
    active_reasoning: str
    review_model: ModelChoice | None
    review_reasoning: str
    consult_model: ModelChoice | None
    consult_reasoning: str
    max_reply_tokens: int

    # Modes
    active_enabled: bool
    active_cadence: str
    active_every_n: int
    review_enabled: bool
    review_authority: str
    review_max_holds: int
    review_evidence_max_chars: int
    decide_enabled: bool
    decide_backend: str
    decide_model: str
    decide_min_confidence: float
    decide_max_calls_per_turn: int
    consult_max_calls_per_turn: int

    # Delivery
    delivery_policy: str
    delivery_max_corrective_per_request: int
    delivery_show_cards: bool
    delivery_note_ttl_turns: int

    # Budget
    budget_session_usd: float
    fallback_price_per_mtok: float
    budget_on_exceed: str

    # Guidance
    guidance_enabled: bool

    # Advanced
    worker_timeout_s: int
    ledger_max_tool_result_chars: int
    ledger_max_recent_turns: int
    ledger_note_max_chars: int
    ledger_max_tokens: int
    skip_same_model: bool
    allow_in_subagents: bool
    debug_log_decisions: bool
    # Secret: excluded from repr so keys never leak into logs or tracebacks.
    decide_typesafe_api_key: str = field(default="", repr=False)

    def model_for(self, mode: str) -> ModelChoice | None:
        """Return the mode override, or the shared coach model when unset."""
        field = _MODE_MODEL_FIELDS.get(mode)
        if field is None:
            return None
        return getattr(self, field) or self.coach_model

    def reasoning_for(self, mode: str) -> str:
        """The mode's level, else the shared one, else "" (the app's level)."""
        field = _REASONING_FIELDS.get(mode)
        if field is None:
            return self.reasoning_effort
        return getattr(self, field) or self.reasoning_effort

    def is_configured(self) -> bool:
        """True when any model is available.

        A per-mode override on its own is a valid setup: the other modes report
        themselves unconfigured when asked, without blocking the one that has a
        model.
        """
        return any(
            choice is not None
            for choice in (
                self.coach_model,
                self.active_model,
                self.review_model,
                self.consult_model,
            )
        )

    def same_as_primary(
        self, choice: ModelChoice | None, model: str, provider: str
    ) -> bool:
        """Compare a resolved choice with the primary model, ignoring case."""
        if choice is None or not model:
            return False
        same_model = choice.model.strip().lower() == model.strip().lower()
        if not provider:
            # Hermes may not expose the primary's provider (auto-detected).
            # The model name is then the only signal we have.
            return same_model
        return same_model and choice.provider.strip().lower() == provider.strip().lower()


_SPLIT_CONVENTION = (
    "# Model ids can contain slashes (org/model), so no split is universally\n"
    "# right; where a parse is unavoidable (a host payload or CLI input that\n"
    "# carries no provider field — AGENTS.md forbids storing split strings),\n"
    "# the codebase convention is that the provider is the FIRST segment,\n"
    "# matching provider-prefixed ids like openrouter/<org>/<model>.\n"
)


def split_provider_model(value: str) -> tuple[str, str, bool]:
    """Split one provider-less ``provider/model`` string on the first slash.

    Use only where the source carries no separate provider field; the bool
    reports whether a slash existed at all. See ``_SPLIT_CONVENTION``.
    """
    head, sep, tail = str(value or "").partition("/")
    return head.strip(), tail.strip(), bool(sep)


_MINIMUMS: dict[str, float] = {
    "max_reply_tokens": 80,
    "active_every_n": 2,
    # 0 is a valid mode: the gate never holds (used < max(0, holds)); the
    # desktop page offers it and README troubleshooting points to it.
    "review_max_holds": 0,
    "review_evidence_max_chars": 2000,
    "decide_max_calls_per_turn": 1,
    "consult_max_calls_per_turn": 1,
    "delivery_max_corrective_per_request": 0,
    "delivery_note_ttl_turns": 0,
    "budget_session_usd": 0.0,
    "fallback_price_per_mtok": 0.0,
    "worker_timeout_s": 5,
    "ledger_max_tool_result_chars": 50,
    "ledger_max_recent_turns": 1,
    "ledger_note_max_chars": 100,
    "ledger_max_tokens": 500,
}

_MAXIMUMS: dict[str, float] = {
    "max_reply_tokens": 2000,
    "active_every_n": 100,
    "review_max_holds": 10,
    "review_evidence_max_chars": 160000,
    "decide_max_calls_per_turn": 10,
    "consult_max_calls_per_turn": 10,
    "delivery_max_corrective_per_request": 10,
    "delivery_note_ttl_turns": 20,
    "budget_session_usd": 1000.0,
    "fallback_price_per_mtok": 100.0,
    "worker_timeout_s": 120,
    "ledger_max_tool_result_chars": 5000,
    "ledger_max_recent_turns": 10,
    "ledger_note_max_chars": 5000,
    "ledger_max_tokens": 20000,
}


def load_settings(raw: Mapping[str, Any] | None = None) -> Settings:
    """Build settings from raw config, falling back on any bad value."""
    data = raw if isinstance(raw, Mapping) else {}
    settings = Settings(
        coach_model=_as_model(data.get("coach_model")),
        reasoning_effort=_as_choice(
            data.get("reasoning_effort"), "reasoning_effort", ""
        ),
        active_model=_as_model(data.get("active_model")),
        active_reasoning=_as_choice(
            data.get("active_reasoning"), "active_reasoning", ""
        ),
        review_model=_as_model(data.get("review_model")),
        review_reasoning=_as_choice(
            data.get("review_reasoning"), "review_reasoning", ""
        ),
        consult_model=_as_model(data.get("consult_model")),
        consult_reasoning=_as_choice(
            data.get("consult_reasoning"), "consult_reasoning", ""
        ),
        max_reply_tokens=_as_int(
            data.get("max_reply_tokens"), DEFAULTS["max_reply_tokens"]
        ),
        active_enabled=_as_bool(
            data.get("active_enabled"), DEFAULTS["active_enabled"]
        ),
        active_cadence=_as_choice(
            data.get("active_cadence"), "active_cadence", DEFAULTS["active_cadence"]
        ),
        active_every_n=_as_int(data.get("active_every_n"), DEFAULTS["active_every_n"]),
        review_enabled=_as_bool(
            data.get("review_enabled"), DEFAULTS["review_enabled"]
        ),
        review_authority=_as_choice(
            data.get("review_authority"),
            "review_authority",
            DEFAULTS["review_authority"],
        ),
        review_max_holds=_as_int(
            data.get("review_max_holds"), DEFAULTS["review_max_holds"]
        ),
        review_evidence_max_chars=_as_int(
            data.get("review_evidence_max_chars"),
            DEFAULTS["review_evidence_max_chars"],
        ),
        decide_enabled=_as_bool(
            data.get("decide_enabled"), DEFAULTS["decide_enabled"]
        ),
        decide_backend=_as_choice(
            data.get("decide_backend"), "decide_backend", DEFAULTS["decide_backend"]
        ),
        decide_model=_as_text(data.get("decide_model"), DEFAULTS["decide_model"]),
        decide_min_confidence=_as_confidence(data.get("decide_min_confidence")),
        decide_max_calls_per_turn=_as_int(
            data.get("decide_max_calls_per_turn"),
            DEFAULTS["decide_max_calls_per_turn"],
        ),
        consult_max_calls_per_turn=_as_int(
            data.get("consult_max_calls_per_turn"),
            DEFAULTS["consult_max_calls_per_turn"],
        ),
        decide_typesafe_api_key=_as_text(
            data.get("decide_typesafe_api_key"), DEFAULTS["decide_typesafe_api_key"]
        ),
        delivery_policy=_as_choice(
            data.get("delivery_policy"), "delivery_policy", DEFAULTS["delivery_policy"]
        ),
        delivery_max_corrective_per_request=_as_int(
            data.get("delivery_max_corrective_per_request"),
            DEFAULTS["delivery_max_corrective_per_request"],
        ),
        delivery_show_cards=_as_bool(
            data.get("delivery_show_cards"), DEFAULTS["delivery_show_cards"]
        ),
        delivery_note_ttl_turns=_as_int(
            data.get("delivery_note_ttl_turns"), DEFAULTS["delivery_note_ttl_turns"]
        ),
        fallback_price_per_mtok=_as_float(
            data.get("fallback_price_per_mtok"),
            DEFAULTS["fallback_price_per_mtok"],
        ),
        budget_session_usd=_as_float(
            data.get("budget_session_usd"), DEFAULTS["budget_session_usd"]
        ),
        budget_on_exceed=_as_choice(
            data.get("budget_on_exceed"), "budget_on_exceed", DEFAULTS["budget_on_exceed"]
        ),
        guidance_enabled=_as_bool(
            data.get("guidance_enabled"), DEFAULTS["guidance_enabled"]
        ),
        worker_timeout_s=_as_int(
            data.get("worker_timeout_s"), DEFAULTS["worker_timeout_s"]
        ),
        ledger_max_tool_result_chars=_as_int(
            data.get("ledger_max_tool_result_chars"),
            DEFAULTS["ledger_max_tool_result_chars"],
        ),
        ledger_max_recent_turns=_as_int(
            data.get("ledger_max_recent_turns"), DEFAULTS["ledger_max_recent_turns"]
        ),
        ledger_note_max_chars=_as_int(
            data.get("ledger_note_max_chars"), DEFAULTS["ledger_note_max_chars"]
        ),
        ledger_max_tokens=_as_int(
            data.get("ledger_max_tokens"), DEFAULTS["ledger_max_tokens"]
        ),
        skip_same_model=_as_bool(
            data.get("skip_same_model"), DEFAULTS["skip_same_model"]
        ),
        allow_in_subagents=_as_bool(
            data.get("allow_in_subagents"), DEFAULTS["allow_in_subagents"]
        ),
        debug_log_decisions=_as_bool(
            data.get("debug_log_decisions"), DEFAULTS["debug_log_decisions"]
        ),
    )
    low = {
        key: minimum
        for key, minimum in _MINIMUMS.items()
        if getattr(settings, key) < minimum
    }
    high = {
        key: maximum
        for key, maximum in _MAXIMUMS.items()
        if getattr(settings, key) > maximum
    }
    clamped = {**low, **high}
    return replace(settings, **clamped) if clamped else settings
