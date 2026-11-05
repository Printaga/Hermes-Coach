"""Settings for the coach plugin.

``DEFAULTS`` is the source of truth for values. ``plugin.yaml`` mirrors it, and
``tests/test_manifest.py`` fails if the two drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["DEFAULTS", "ModelChoice", "Settings", "load_settings"]


@dataclass(frozen=True)
class ModelChoice:
    """The provider and model one mode should call."""

    provider: str
    model: str


DEFAULTS: dict[str, Any] = {
    # Models
    "coach_model": None,
    "active_model": None,
    "review_model": None,
    "consult_model": None,
    "max_reply_tokens": 400,
    # Modes
    "active_enabled": False,
    "active_cadence": "tool_turns_only",
    "active_every_n": 3,
    "review_enabled": True,
    "review_authority": "hold_once",
    "review_max_holds": 1,
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
    "budget_session_usd": 0.50,
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
    "debug_log_decisions": False,
}

_CHOICES: dict[str, tuple[str, ...]] = {
    "active_cadence": ("tool_turns_only", "every_turn", "every_n"),
    "review_authority": ("hold_once", "report"),
    "decide_backend": ("jev-openrouter", "jev-direct", "chat-model"),
    "delivery_policy": ("severity", "ambient", "corrective"),
    "budget_on_exceed": ("warn", "soft", "hard"),
}

_MODE_MODEL_FIELDS: dict[str, str] = {
    "consult": "consult_model",
    "active": "active_model",
    "review": "review_model",
}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else default
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


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
    provider = str(value.get("provider") or "").strip()
    model = str(value.get("model") or "").strip()
    if not provider or not model:
        return None
    return ModelChoice(provider=provider, model=model)


@dataclass(frozen=True)
class Settings:
    """Validated, immutable settings for one coach turn."""

    # Models
    coach_model: ModelChoice | None
    active_model: ModelChoice | None
    review_model: ModelChoice | None
    consult_model: ModelChoice | None
    max_reply_tokens: int

    # Modes
    active_enabled: bool
    active_cadence: str
    active_every_n: int
    review_enabled: bool
    review_authority: str
    review_max_holds: int
    decide_enabled: bool
    decide_backend: str
    decide_model: str
    decide_min_confidence: float
    decide_max_calls_per_turn: int
    consult_max_calls_per_turn: int
    decide_typesafe_api_key: str

    # Delivery
    delivery_policy: str
    delivery_max_corrective_per_request: int
    delivery_show_cards: bool
    delivery_note_ttl_turns: int

    # Budget
    budget_session_usd: float
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
    debug_log_decisions: bool

    def model_for(self, mode: str) -> ModelChoice | None:
        """Return the mode override, or the shared coach model when unset."""
        field = _MODE_MODEL_FIELDS.get(mode)
        if field is None:
            return None
        return getattr(self, field) or self.coach_model

    def is_configured(self) -> bool:
        """True once a shared coach model has been chosen."""
        return self.coach_model is not None

    def same_as_primary(
        self, choice: ModelChoice | None, model: str, provider: str
    ) -> bool:
        """Compare a resolved choice with the primary model, ignoring case."""
        if choice is None or not model or not provider:
            return False
        return (
            choice.model.strip().lower() == model.strip().lower()
            and choice.provider.strip().lower() == provider.strip().lower()
        )


def load_settings(raw: Mapping[str, Any] | None = None) -> Settings:
    """Build settings from raw config, falling back on any bad value."""
    data = raw if isinstance(raw, Mapping) else {}
    return Settings(
        coach_model=_as_model(data.get("coach_model")),
        active_model=_as_model(data.get("active_model")),
        review_model=_as_model(data.get("review_model")),
        consult_model=_as_model(data.get("consult_model")),
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
        decide_enabled=_as_bool(
            data.get("decide_enabled"), DEFAULTS["decide_enabled"]
        ),
        decide_backend=_as_choice(
            data.get("decide_backend"), "decide_backend", DEFAULTS["decide_backend"]
        ),
        decide_model=str(data.get("decide_model") or DEFAULTS["decide_model"]).strip()
        or DEFAULTS["decide_model"],
        decide_min_confidence=_as_confidence(data.get("decide_min_confidence")),
        decide_max_calls_per_turn=_as_int(
            data.get("decide_max_calls_per_turn"),
            DEFAULTS["decide_max_calls_per_turn"],
        ),
        consult_max_calls_per_turn=_as_int(
            data.get("consult_max_calls_per_turn"),
            DEFAULTS["consult_max_calls_per_turn"],
        ),
        decide_typesafe_api_key=str(
            data.get("decide_typesafe_api_key")
            if data.get("decide_typesafe_api_key") is not None
            else DEFAULTS["decide_typesafe_api_key"]
        ).strip(),
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
        debug_log_decisions=_as_bool(
            data.get("debug_log_decisions"), DEFAULTS["debug_log_decisions"]
        ),
    )
