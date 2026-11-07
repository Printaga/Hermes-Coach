# tests/test_settings.py
from coach.settings import DEFAULTS, load_settings


def test_defaults_are_unconfigured():
    s = load_settings({})
    assert s.is_configured() is False
    assert s.model_for("consult") is None
    assert s.active_enabled is False
    assert s.review_enabled is True
    assert s.delivery_policy == "severity"

def test_per_mode_override_inherits_default():
    s = load_settings({"coach_model": {"provider": "openrouter", "model": "a/b"}})
    assert s.is_configured() is True
    assert s.model_for("consult").model == "a/b"
    s2 = load_settings({"coach_model": {"provider": "openrouter", "model": "a/b"},
                        "review_model": {"provider": "anthropic", "model": "c"}})
    assert s2.model_for("review").provider == "anthropic"
    assert s2.model_for("active").model == "a/b"

def test_bad_values_fall_back_to_defaults():
    s = load_settings({"active_every_n": "nope", "decide_min_confidence": 3.0,
                       "budget_on_exceed": "explode"})
    assert s.active_every_n == DEFAULTS["active_every_n"]
    assert s.decide_min_confidence == 1.0
    assert s.budget_on_exceed == "soft"


def test_same_model_matches_when_the_primary_provider_is_unknown():
    s = load_settings({"coach_model": {"provider": "openrouter", "model": "m"}})
    assert s.same_as_primary(s.coach_model, model="m", provider="") is True
    assert s.same_as_primary(s.coach_model, model="other", provider="") is False


def test_a_per_mode_model_alone_counts_as_configured():
    s = load_settings({"consult_model": {"provider": "p", "model": "m"}})
    assert s.is_configured() is True
    assert s.model_for("consult").model == "m"
    assert s.model_for("active") is None


def test_coercion_policies_reject_bools_and_non_finite():
    from coach.coerce import as_bool, as_finite_float, as_int, as_text

    assert as_finite_float(True) == 0.0  # jev used to price True as 1.0
    assert as_finite_float("1.5") == 1.5
    assert as_finite_float(float("inf")) == 0.0
    assert as_finite_float(10**400) == 0.0
    assert as_int(True) == 0
    assert as_int(2.0) == 2
    assert as_int(2.5) == 0
    assert as_bool("ON") is True
    assert as_bool("maybe", default=False) is False
    assert as_text("  x  ") == "x"
    assert as_text(None, default="d") == "d"


def test_reasoning_defaults_to_inherit_and_resolves_per_mode():
    from coach.settings import REASONING_LEVELS, load_settings

    settings = load_settings({"coach_model": {"provider": "p", "model": "m"}})
    assert settings.reasoning_effort == ""
    assert settings.reasoning_for("review") == ""
    assert "" in REASONING_LEVELS and "high" in REASONING_LEVELS

    settings = load_settings(
        {
            "coach_model": {"provider": "p", "model": "m"},
            "reasoning_effort": "low",
            "review_reasoning": "xhigh",
        }
    )
    assert settings.reasoning_for("active") == "low"   # inherits the shared level
    assert settings.reasoning_for("review") == "xhigh"  # mode override
    assert settings.reasoning_for("consult") == "low"


def test_an_unknown_reasoning_level_falls_back_to_inherit():
    from coach.settings import load_settings

    settings = load_settings(
        {"coach_model": {"provider": "p", "model": "m"}, "reasoning_effort": "hgih"}
    )
    assert settings.reasoning_effort == ""


def test_every_reasoning_setting_is_a_choice():
    from coach.settings import _CHOICES

    for key in ("reasoning_effort", "active_reasoning", "review_reasoning", "consult_reasoning"):
        assert key in _CHOICES
