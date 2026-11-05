# tests/test_plugin_decide.py
import json

from coach.jev import Decision
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.test_plugin_ask import Llm

ARGS = {"question": "Which path?", "options": ["a", "b"], "turn_id": "t1"}


def _p(tmp_path, **cfg):
    base = {"coach_model": {"provider": "p", "model": "m"}, "decide_enabled": True}
    base.update(cfg)
    return CoachPlugin(
        llm=Llm(), store=Store(tmp_path), settings=load_settings(base)
    )


def _fake(decision=None, captured=None):
    def dec(**kw):
        if captured is not None:
            captured.update(kw)
        return decision or Decision("b", 0.8, {"a": 0.2, "b": 0.8}, "act", "cheaper", 0.0004)

    return dec


def test_disabled_refuses_without_calling(tmp_path, monkeypatch):
    p = _p(tmp_path, decide_enabled=False)
    monkeypatch.setattr(
        "coach.plugin.decide",
        lambda **kw: (_ for _ in ()).throw(AssertionError("should not call")),
    )
    out = json.loads(p.tool_decide(ARGS))
    assert out["ok"] is False and out["error"] == "decide_disabled"


def test_decision_is_returned_and_spend_recorded(tmp_path, monkeypatch):
    p = _p(tmp_path)
    captured = {}
    monkeypatch.setattr("coach.plugin.decide", _fake(captured=captured))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    out = json.loads(p.tool_decide(ARGS))
    assert out["ok"] is True
    assert out["decision"] == "b" and out["gate"] == "act"
    assert out["probabilities"]["b"] == 0.8
    assert out["cost_usd"] == 0.0004
    assert p.store.load("current").spend_usd == 0.0004
    assert captured["api_key"] == "k"


def test_option_count_is_bounded(tmp_path, monkeypatch):
    p = _p(tmp_path)
    monkeypatch.setattr("coach.plugin.decide", _fake())
    assert json.loads(p.tool_decide({"question": "q", "options": ["a"]}))["ok"] is False
    many = [f"o{i}" for i in range(7)]
    assert json.loads(p.tool_decide({"question": "q", "options": many}))["ok"] is False


def test_per_turn_call_cap(tmp_path, monkeypatch):
    p = _p(tmp_path)
    monkeypatch.setattr("coach.plugin.decide", _fake())
    for _ in range(3):
        assert json.loads(p.tool_decide(ARGS))["ok"] is True
    assert json.loads(p.tool_decide(ARGS))["error"] == "call_cap"


def test_soft_budget_stops_decide(tmp_path, monkeypatch):
    p = _p(tmp_path, budget_session_usd=0.01, budget_on_exceed="soft")
    state = p.store.load("current")
    state.spend_usd = 1.0
    p.store.save(state)
    monkeypatch.setattr("coach.plugin.decide", _fake())
    assert json.loads(p.tool_decide(ARGS))["error"] == "budget_exhausted"


def test_child_session_is_refused(tmp_path, monkeypatch):
    p = CoachPlugin(
        llm=Llm(),
        store=Store(tmp_path),
        settings=load_settings(
            {"coach_model": {"provider": "p", "model": "m"}, "decide_enabled": True}
        ),
        surface=lambda: "subagent",
    )
    monkeypatch.setattr("coach.plugin.decide", _fake())
    assert json.loads(p.tool_decide(ARGS))["error"] == "child_session"


def test_chat_backend_receives_the_coach(tmp_path, monkeypatch):
    p = _p(tmp_path, decide_backend="chat-model")
    captured = {}
    monkeypatch.setattr("coach.plugin.decide", _fake(captured=captured))
    assert json.loads(p.tool_decide(ARGS))["ok"] is True
    assert captured["chat"] is p.coach


def test_direct_backend_uses_configured_key(tmp_path, monkeypatch):
    p = _p(tmp_path, decide_backend="jev-direct", decide_typesafe_api_key="type-key")
    captured = {}
    monkeypatch.setattr("coach.plugin.decide", _fake(captured=captured))
    assert json.loads(p.tool_decide(ARGS))["ok"] is True
    assert captured["api_key"] == "type-key"


def test_error_gate_is_reported_and_recorded(tmp_path, monkeypatch):
    p = _p(tmp_path)
    monkeypatch.setattr(
        "coach.plugin.decide",
        _fake(Decision("", 0.0, {}, "error", "endpoint down", 0.0)),
    )
    out = json.loads(p.tool_decide(ARGS))
    assert out["ok"] is False and out["error"] == "endpoint down"
    assert p.store.load("current").last_error == "endpoint down"
