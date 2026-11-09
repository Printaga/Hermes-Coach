# Cross-cutting regression tests for issues found while building the plugin.
#
# Each test pins one behavior that broke, misled, or silently degraded the
# plugin during development or live verification. Unit tests next to each
# module cover the happy paths; these tests cover the failure modes.

from pathlib import Path

from coach.commands import _coerce_setting, _parse_model
from coach.plugin import (
    CoachPlugin,
)
from coach.settings import load_settings
from coach.state import Store
from tests.support import AdvisorLlm as _AdvisorLlm
from tests.support import ConsultLlm as _ConsultLlm
from tests.support import FakeWorker as _FakeWorker

REPO = Path(__file__).resolve().parent.parent


def _plugin(tmp_path, **config):
    base = {"coach_model": {"provider": "openrouter", "model": "x"}}
    base.update(config)
    return CoachPlugin(
        llm=_ConsultLlm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings(base),
    )


# ---------------------------------------------------------------------------
# Hooks, commands, and registration
# ---------------------------------------------------------------------------


def test_post_tool_calls_are_ignored_when_active_is_off(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=False)
    plugin.on_post_tool_call(
        session_id="s",
        turn_id="t",
        tool_name="read_file",
        args={"path": "a.py"},
        result="ok",
    )
    assert plugin._turn_tools == {}


def test_post_llm_call_records_intent_for_review_without_active(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=False, review_enabled=True)
    plugin.on_post_llm_call(
        session_id="s",
        turn_id="t1",
        user_message="Add a retry cap",
        assistant_response="ok",
        conversation_history=[],
        model="m",
    )
    assert plugin.store.load("s").last_user_message == "Add a retry cap"


def test_queued_active_review_is_replaced_by_a_newer_turn(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=True)
    plugin.worker = _FakeWorker()
    plugin.on_post_tool_call(
        session_id="s",
        turn_id="t1",
        tool_name="read_file",
        args={"path": "a.py"},
        result="ok",
    )
    plugin.on_post_llm_call(
        session_id="s",
        turn_id="t1",
        user_message="u",
        assistant_response="a",
        conversation_history=[],
        model="m",
    )
    plugin.on_post_tool_call(
        session_id="s",
        turn_id="t2",
        tool_name="read_file",
        args={"path": "b.py"},
        result="ok",
    )
    plugin.on_post_llm_call(
        session_id="s",
        turn_id="t2",
        user_message="u",
        assistant_response="a",
        conversation_history=[],
        model="m",
    )
    assert plugin.worker.submitted == [("s", "t1"), ("s", "t2")]
    assert set(plugin._queued) == {("s", "t2")}


def test_unconfigured_plugin_never_queues_active_reviews(tmp_path):
    plugin = CoachPlugin(
        llm=_ConsultLlm(),
        store=Store(tmp_path),
        settings=load_settings({"active_enabled": True}),
    )
    state = plugin.store.load("s")
    assert (
        plugin.should_review(
            state,
            {"turn_id": "t1", "tools": [{"name": "read_file"}]},
            plugin.settings,
        )
        is False
    )


def test_command_rejects_bad_modes_models_and_settings(tmp_path):
    plugin = _plugin(tmp_path)
    assert "Usage" in plugin.command("model bogus p/m")
    assert "Use provider/model" in plugin.command("model consult nope")
    assert "Usage" in plugin.command("active")
    assert "Unknown setting" in plugin.command("set nope 1")
    assert "Invalid value" in plugin.command("set active_every_n nope")


def test_command_can_clear_a_per_mode_model(tmp_path):
    plugin = _plugin(
        tmp_path, review_model={"provider": "p", "model": "m"}
    )
    out = plugin.command("model review clear")
    assert "none set" in out.lower()
    assert plugin.settings.review_model is None


def test_model_command_rejects_unparseable_values(tmp_path):
    assert _parse_model("openrouter/")[0] is False
    assert _parse_model("/model")[0] is False
    assert _parse_model("just-a-name")[0] is False
    assert _coerce_setting("active_enabled", "maybe") == (False, None)


def test_pre_llm_call_returns_guidance_when_timing_pre_and_first_turn(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=True, active_timing="pre")
    out = plugin.on_pre_llm_call(session_id="s", user_message="implement migration X")
    assert out is not None and out.get("context")


def test_pre_only_timing_disables_post_turn_review(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=True, active_timing="pre")
    state = plugin.store.load("s")
    assert (
        plugin.should_review(
            state,
            {"turn_id": "t1", "tools": [{"name": "read_file"}]},
            plugin.settings,
        )
        is False
    )


def test_pre_turn_skips_blank_user_text(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=True, active_timing="pre")
    assert plugin.on_pre_llm_call(session_id="s", user_message="   ") is None


def test_pre_turn_fail_open_records_skip_on_provider_error(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=True, active_timing="pre")

    class FailingLlm(_AdvisorLlm):
        def complete(self, messages, **kwargs):
            raise RuntimeError("provider down")

    plugin.coach.llm = FailingLlm()
    out = plugin.on_pre_llm_call(session_id="s", user_message="implement migration X")
    assert out is None or not out.get("context")
    assert plugin.store.load("s").calls.get("review_skips") == 1
