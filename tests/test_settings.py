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
