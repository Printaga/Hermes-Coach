# tests/test_secrets.py
"""Decide credentials come through the host's profile-scoped secret scope.

Catalog rule 11: never a bare ``os.environ`` read (it can pick up another
profile's key under multiplexing). ``coach.secrets.get_secret`` prefers the
host scope, falls back to the process env only on hosts that predate the
seam, and never raises out of a tool handler.
"""

import json
import sys
import types

from coach.jev import Decision
from coach.plugin import CoachPlugin
from coach.secrets import get_secret
from coach.settings import load_settings
from coach.state import Store
from tests.support import ConsultLlm as Llm


def _block_host(monkeypatch):
    """Make ``agent.secret_scope`` unimportable: a pre-seam host."""
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)


def _fake_host(monkeypatch, mapping=None, explode=False):
    """Install a fake ``agent.secret_scope`` with a profile mapping."""
    module = types.ModuleType("agent.secret_scope")
    mapping = mapping or {}

    def fake_get(name, default=None):
        if explode:
            raise RuntimeError("no scope bound")
        return mapping.get(name, default)

    module.get_secret = fake_get
    package = types.ModuleType("agent")
    package.__path__ = []
    package.secret_scope = module
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)


def test_falls_back_to_process_env_without_host_seam(monkeypatch):
    _block_host(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert get_secret("OPENROUTER_API_KEY") == "env-key"


def test_missing_key_is_empty_without_host_seam(monkeypatch):
    _block_host(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert get_secret("OPENROUTER_API_KEY") == ""


def test_host_scope_wins_over_process_env(monkeypatch):
    # The multiplexing case: the process env holds another profile's value.
    _fake_host(monkeypatch, {"OPENROUTER_API_KEY": "scoped-key"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "other-profile-key")
    assert get_secret("OPENROUTER_API_KEY") == "scoped-key"


def test_host_failure_is_empty_never_raises(monkeypatch):
    _fake_host(monkeypatch, explode=True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert get_secret("OPENROUTER_API_KEY") == ""


def _plugin(tmp_path, **cfg):
    base = {"coach_model": {"provider": "p", "model": "m"}, "decide_enabled": True}
    base.update(cfg)
    return CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings(base))


def test_tool_decide_uses_scoped_key_end_to_end(tmp_path, monkeypatch):
    _fake_host(monkeypatch, {"OPENROUTER_API_KEY": "scoped-key"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "other-profile-key")
    captured = {}

    def dec(**kw):
        captured.update(kw)
        return Decision("b", 0.8, {"a": 0.2, "b": 0.8}, "act", "cheaper", 0.0)

    monkeypatch.setattr("coach.plugin.decide", dec)
    out = json.loads(
        _plugin(tmp_path).tool_decide(
            {"question": "Which path?", "options": ["a", "b"], "turn_id": "t1"}
        )
    )
    assert out["ok"] is True
    assert captured["api_key"] == "scoped-key"


def test_direct_backend_uses_scoped_key_end_to_end(tmp_path, monkeypatch):
    _fake_host(monkeypatch, {"TYPESAFE_API_KEY": "scoped-type-key"})
    monkeypatch.setenv("TYPESAFE_API_KEY", "other-profile-key")
    captured = {}

    def dec(**kw):
        captured.update(kw)
        return Decision("a", 0.9, {"a": 0.9, "b": 0.1}, "act", "cheaper", 0.0)

    monkeypatch.setattr("coach.plugin.decide", dec)
    out = json.loads(
        _plugin(tmp_path, decide_backend="jev-direct").tool_decide(
            {"question": "Which path?", "options": ["a", "b"], "turn_id": "t1"}
        )
    )
    assert out["ok"] is True
    assert captured["api_key"] == "scoped-type-key"


def test_explicit_setting_still_wins_over_scope(tmp_path, monkeypatch):
    _fake_host(monkeypatch, {"TYPESAFE_API_KEY": "scoped-type-key"})
    captured = {}

    def dec(**kw):
        captured.update(kw)
        return Decision("a", 0.9, {"a": 0.9, "b": 0.1}, "act", "cheaper", 0.0)

    monkeypatch.setattr("coach.plugin.decide", dec)
    out = json.loads(
        _plugin(
            tmp_path,
            decide_backend="jev-direct",
            decide_typesafe_api_key="explicit-key",
        ).tool_decide(
            {"question": "Which path?", "options": ["a", "b"], "turn_id": "t1"}
        )
    )
    assert out["ok"] is True
    assert captured["api_key"] == "explicit-key"
