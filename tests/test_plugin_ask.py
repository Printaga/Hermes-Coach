# tests/test_plugin_ask.py
import json

import pytest

from coach.guidance import Guidance
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.support import ConsultLlm as Llm


def _plugin(tmp_path, *, settings=None, surface=lambda: "cli"):
    return CoachPlugin(llm=Llm(), store=Store(tmp_path), surface=surface,
                       settings=settings or load_settings(
                           {"coach_model": {"provider": "openrouter", "model": "x"}}))

def test_ask_returns_text_as_json_string(tmp_path):
    out = json.loads(_plugin(tmp_path).tool_ask({"question": "Is this parser ok?", "intent": "advice"}))
    assert out["ok"] is True and out["advice"].startswith("Advice body.")

def test_unconfigured_answers_clearly(tmp_path):
    p = CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings({}))
    out = json.loads(p.tool_ask({"question": "q"}))
    assert out["ok"] is False and "no coach model" in out["error"]
    assert p.status()["configured"] is False

def test_child_sessions_are_refused(tmp_path):
    p = _plugin(tmp_path, surface=lambda: "subagent")
    out = json.loads(p.tool_ask({"question": "q"}))
    assert out["ok"] is False and out["error"] == "child_session"

def test_per_turn_cap(tmp_path):
    p = _plugin(tmp_path)
    args = {"question": "q", "turn_id": "t1"}
    for _ in range(4):
        assert json.loads(p.tool_ask(args))["ok"] is True
    assert json.loads(p.tool_ask(args))["error"] == "call_cap"

def test_budget_hard_stop_refuses_consult(tmp_path):
    p = _plugin(tmp_path, settings=load_settings(
        {"coach_model": {"provider": "openrouter", "model": "x"},
         "budget_session_usd": 0.01, "budget_on_exceed": "hard"}))
    st = p.store.load("current"); st.spend_usd = 1.0; p.store.save(st)
    assert json.loads(p.tool_ask({"question": "q"}))["error"] == "budget_exhausted"

def test_subagent_hooks_track_child_sessions(tmp_path):
    p = _plugin(tmp_path)
    p.on_subagent_start(session_id="child-1")
    refused = json.loads(p.tool_ask({"question": "q"}, session_id="child-1"))
    assert refused["ok"] is False and refused["error"] == "child_session"
    p.on_subagent_stop(session_id="child-1")
    assert json.loads(p.tool_ask({"question": "q"}, session_id="child-1"))["ok"] is True

def test_allow_in_subagents_bypasses_both_guards(tmp_path):
    p = _plugin(tmp_path, surface=lambda: "subagent", settings=load_settings(
        {"coach_model": {"provider": "openrouter", "model": "x"},
         "allow_in_subagents": True}))
    p.on_subagent_start(session_id="child-1")
    assert json.loads(p.tool_ask({"question": "q"}, session_id="child-1"))["ok"] is True

def test_guidance_block_wraps_global_and_project_files(tmp_path):
    root = tmp_path / "coach"
    project = tmp_path / "project"
    root.mkdir(); project.mkdir()
    (root / "guidance.md").write_text("Be blunt.", encoding="utf-8")
    (project / "COACH.md").write_text("Use tabs.", encoding="utf-8")
    block = Guidance(root).system_block(str(project))
    assert block.startswith("<coach-guidance>") and block.endswith("</coach-guidance>")
    assert "Be blunt." in block and "Use tabs." in block


def test_settings_saved_by_the_page_apply_without_a_reload(tmp_path):
    raw = {}
    p = CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings({}),
                    settings_reader=lambda: raw)
    assert p.status()["configured"] is False
    raw["coach_model"] = {"provider": "openrouter", "model": "x"}
    assert p.status()["configured"] is True
    assert json.loads(p.tool_ask({"question": "q"}))["ok"] is True


def test_host_ids_reach_the_tool(tmp_path):
    p = _plugin(tmp_path)
    out = json.loads(p.tool_ask({"question": "q"}, session_id="s1", task_id="task-1"))
    assert out["ok"] is True
    state = p.store.load("s1")
    # Consult keeps its own namespace so a turn id can never alias a global
    # telemetry counter (review_skips, decide_unpriced, ...).
    assert state.calls.get("consult:task-1") == 1


def test_hook_model_becomes_the_same_model_baseline(tmp_path):
    p = _plugin(tmp_path)
    p.on_pre_llm_call(session_id="s", turn_id="t", conversation_history=[], model="glm-5.3")
    p.settings = load_settings({"coach_model": {"provider": "opencode-go", "model": "glm-5.3"}})
    p.coach.settings = p.settings
    assert p.coach.check("consult") == "same_model"


def test_same_model_guard_reads_the_session_model(tmp_path):
    p = _plugin(tmp_path, settings=load_settings(
        {"coach_model": {"provider": "opencode-go", "model": "glm-5.3"}}))
    p.on_pre_llm_call(session_id="s", turn_id="t", conversation_history=[], model="glm-5.3")
    out = json.loads(p.tool_ask({"question": "q"}, session_id="s", task_id="t2"))
    assert out["ok"] is False and "matches the primary" in out["error"].lower()


def test_consult_posts_a_card_with_the_advice(tmp_path):
    p = _plugin(tmp_path)
    posted = []
    p.post = posted.append
    result = json.loads(p.tool_ask({"question": "Is the retry safe?", "session_id": "s"}))
    assert result["ok"] is True
    assert posted and "Coach advice" in posted[0]
    assert "advice" in posted[0].lower()


def test_advice_card_is_suppressed_when_cards_are_off(tmp_path):
    p = _plugin(tmp_path, settings=load_settings({"coach_model": {"provider": "openrouter", "model": "x"}, "delivery_show_cards": False}))
    posted = []
    p.post = posted.append
    p.tool_ask({"question": "Is the retry safe?", "session_id": "s"})
    assert posted == []


def test_advice_card_truncates_a_huge_answer(tmp_path):
    from coach.plugin import _advice_card

    card = _advice_card("x" * 5000, "advice", "p", "m")
    assert len(card) < 1200
    assert " ..." in card


def test_pre_llm_call_records_the_working_directory(tmp_path):
    p = _plugin(tmp_path, surface=lambda: "cli")
    p.home = tmp_path / "coach"
    p.cwd = lambda: str(tmp_path)
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t"})
    data = json.loads((tmp_path / "coach" / "state.json").read_text())
    assert str(tmp_path) in data["seen_cwds"]


def test_a_raised_consult_call_releases_the_reservation(tmp_path, monkeypatch):
    p = _plugin(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(p.coach, "ask", boom)
    with pytest.raises(RuntimeError):
        p._ask({"question": "q", "turn_id": "t1", "session_id": "s"})
    assert p.store.load("s").calls.get("consult:t1", 0) == 0


def test_a_turn_named_like_a_counter_does_not_corrupt_status(tmp_path):
    p = _plugin(tmp_path)
    assert json.loads(p.tool_ask({"question": "q", "turn_id": "review_skips"}))["ok"] is True
    assert p.status()["review_skips"] == 0
