"""Command and tool surfaces: the parts of the plugin a host calls directly."""

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from coach.evidence import collect
from coach.findings import Finding
from coach.plugin import _primary_identity
from coach.state import Store
from tests.support import plugin

def test_grant_writes_both_trust_flags(tmp_path, monkeypatch):
    import sys
    import types

    saved = {}
    parent = types.ModuleType("hermes_cli")
    parent.__path__ = []
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config = lambda: {}
    config_module.save_config = lambda config: saved.update(
        copy.deepcopy(config)
    ) or True
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setattr(parent, "config", config_module, raising=False)

    assert "Granted" in plugin(tmp_path).command("grant")
    llm = saved["plugins"]["entries"]["coach"]["llm"]
    assert llm == {"allow_provider_override": True, "allow_model_override": True}


def test_primary_identity_prefers_live_hook_model(tmp_path, monkeypatch):
    import sys
    import types

    parent = types.ModuleType("hermes_cli")
    parent.__path__ = []
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config_readonly = lambda: {
        "model": {"default": "openrouter/m"}
    }
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setattr(parent, "config", config_module, raising=False)
    assert _primary_identity() == ("m", "openrouter")
    target = plugin(tmp_path)
    target.on_pre_llm_call(
        session_id="s", turn_id="t", conversation_history=[], model="live-model"
    )
    assert target.primary_model == "live-model"


def test_model_command_sets_shows_and_lists(tmp_path):
    target = plugin(tmp_path)
    out = target.command("model consult openrouter/glm-5.3")
    assert out == "Consult model set to openrouter/glm-5.3."
    assert target.settings.consult_model is not None
    assert "Consult model: openrouter/glm-5.3" in target.command("model consult")
    assert "Coach model:" in target.command("model")


def test_command_bare_input_prints_help_and_failures_are_named(tmp_path):
    target = plugin(tmp_path)
    assert target.command("") == target.command("wat")
    assert "Usage" in target.command("model bogus")
    assert target._apply("nope", "x") is False
    refusing = plugin(tmp_path)
    refusing.set_setting = lambda key, value: False
    assert refusing.command("active on") == "Coach: could not save active_enabled."
    assert refusing.command("set review_max_holds 2") == (
        "Coach: could not save review_max_holds."
    )
    assert refusing.command("model consult p/m") == (
        "Coach: could not save consult_model."
    )
    assert refusing.command("set consult_model p/m") == (
        "Coach: could not save consult_model."
    )


def test_post_and_inject_failures_do_not_raise(tmp_path):
    def boom_post(text):
        raise RuntimeError("post down")

    def boom_inject(text):
        raise RuntimeError("inject down")

    quiet = plugin(tmp_path)
    quiet.post = boom_post
    quiet.deliver(Finding("nit", "t", "b", "e", "f"), session_id="s", turn_id="t1")
    assert quiet.store.load("s").pending

    loud = plugin(tmp_path)
    loud.inject = boom_inject
    assert loud._inject("hello") is False


def test_tool_wrappers_return_internal_error_instead_of_raising(
    tmp_path, monkeypatch
):
    import coach.plugin as plugin_module

    target = plugin(tmp_path, decide_enabled=True)

    def boom(**kwargs):
        raise RuntimeError("prompt broke")

    monkeypatch.setattr(plugin_module, "build_messages", boom)
    out = json.loads(target.tool_ask({"question": "q"}))
    assert out["ok"] is False
    assert "internal_error" in out["error"]

    def boom_decide(**kwargs):
        raise RuntimeError("decide broke")

    monkeypatch.setattr(plugin_module, "decide", boom_decide)
    out = json.loads(
        target.tool_decide({"question": "q", "options": ["a", "b"]})
    )
    assert out["ok"] is False
    assert "internal_error" in out["error"]


def test_ask_rejects_empty_and_oversized_questions(tmp_path):
    target = plugin(tmp_path)
    assert json.loads(target.tool_ask({}))["error"] == "question_required"
    assert (
        json.loads(target.tool_ask({"question": "x" * 4001}))["error"]
        == "question_too_long"
    )


def test_unknown_intent_defaults_to_advice_and_context_is_attached(tmp_path):
    target = plugin(tmp_path)
    out = json.loads(
        target.tool_ask(
            {"question": "q", "intent": "bogus", "context": "the diff"},
            session_id="s",
        )
    )
    assert out["ok"] is True and out["intent"] == "advice"
    assert "the diff" in target.llm.messages[-1]["content"]


def test_log_falls_back_to_newest_session_with_a_label(tmp_path):
    other = plugin(tmp_path)
    other._log_review(
        "other",
        "t1",
        SimpleNamespace(
            finding=Finding("nit", "kept", "b", "e", "f"),
            usage=None,
            cost_usd=0.0,
            provider="p",
            model="m",
            error="",
        ),
    )
    target = plugin(tmp_path)
    out = target.command("log", session_id="unknown-session")
    assert "kept" not in out and "(from session" not in out
    assert "No coach reviews logged" in out


def test_error_reviews_render_their_error(tmp_path):
    target = plugin(tmp_path)
    target._log_review(
        "s",
        "t1",
        SimpleNamespace(
            finding=None,
            usage=None,
            cost_usd=0.0,
            provider="p",
            model="m",
            error="provider_error: Timeout",
        ),
    )
    assert "error: provider_error: Timeout" in target.command("log", session_id="s")


def test_evidence_symlink_escape_is_refused(tmp_path):
    outside = tmp_path.parent / "evidence-secret"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    link = tmp_path / "ev-link"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert collect(str(tmp_path), ["ev-link/secret.txt"]) == ""


def test_long_session_ids_do_not_grow_file_names(tmp_path):
    from coach.state import _MAX_NAME_CHARS

    store = Store(tmp_path)
    long_id = "s" * 200
    store.save(store.load(long_id))
    name = store._path(long_id).name
    assert len(name) <= _MAX_NAME_CHARS + 1 + 8 + len(".json")
    assert store.load(long_id).session_id == long_id


def test_guidance_read_truncates_and_names_unknown_scopes(tmp_path):
    from coach.guidance import GUIDANCE_MAX_CHARS, Guidance

    guidance = Guidance(tmp_path)
    # The dashboard route is the only writer; read() still caps what a file
    # of any length contributes to the prompt.
    (tmp_path / "guidance.md").write_text("x" * (GUIDANCE_MAX_CHARS + 10))
    assert len(guidance.read("global", "")) == GUIDANCE_MAX_CHARS
    with pytest.raises(ValueError):
        guidance._path("everywhere", "")
