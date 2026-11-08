# Coverage for small helpers, validation edges, and host-failure branches.
#
# These tests intentionally target pure helpers and failure paths rather than
# repeating the main behavioral suites.

import json
import os
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from coach.advisor import Coach
from coach.commands import _bool_arg
from coach.evidence import _git, _heads, _read_text, _safe_paths, _stdout
from coach.findings import Finding, parse_finding
from coach.ledger import _summary_line, build_messages, tool_card
from coach.plugin import (
    CoachPlugin,
    _as_turn,
    _child_id,
    _find_turn,
    _last_reviewed_digest,
    _log_name,
    _payload,
    _primary_identity,
    _record_turn,
    _request_text,
    _result_text,
    _tool_arg,
    _turns_since_review,
    build_plugin,
)
from coach.settings import (
    DEFAULTS,
    _as_bool,
    load_settings,
)
from coach.state import (
    Store,
    _dict_items,
)
from coach.state import (
    _as_float as state_float,
)
from coach.state import (
    _as_int as state_int,
)
from dashboard import plugin_api as api
from dashboard.plugin_api import app_for_tests
from tests.support import ConsultLlm, StructuredLlm, decide_plugin, plugin


def _hermes_config(monkeypatch, **functions):
    parent = types.ModuleType("hermes_cli")
    parent.__path__ = []
    module = types.ModuleType("hermes_cli.config")
    for name, function in functions.items():
        setattr(module, name, function)
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    monkeypatch.setattr(parent, "config", module, raising=False)
    return module


def test_command_guidance_send_pause_and_model_show(tmp_path):
    from coach.guidance import Guidance

    project = tmp_path / "project"
    project.mkdir()
    coach_plugin = CoachPlugin(
        llm=ConsultLlm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
        guidance=Guidance(tmp_path / "coach"),
        cwd=lambda: str(project),
        post=lambda text: None,
        inject=lambda text: True,
    )
    (tmp_path / "coach").mkdir(parents=True, exist_ok=True)
    (tmp_path / "coach" / "guidance.md").write_text("Be blunt.")
    (project / "COACH.md").write_text("Use tabs.")
    guidance = coach_plugin.command("guidance")
    assert "Be blunt." in guidance and "Use tabs." in guidance

    coach_plugin.deliver(
        Finding("nit", "Spacing", "One space.", "a.py:1", "Drop it."),
        session_id="s",
        turn_id="t1",
    )
    assert "Delivered 1" in coach_plugin.command("send", session_id="s")
    assert coach_plugin.store.load("s").pending == []

    assert "paused" in coach_plugin.command("pause").lower()
    assert "Paused: yes" in coach_plugin.command("status")
    assert "resumed" in coach_plugin.command("pause").lower()

    assert "Coach model" in coach_plugin.command("model")
    assert "Review on." in coach_plugin.command("review on")
    assert "Decide on." in coach_plugin.command("decide on")
    assert "5" in coach_plugin.command("set active_every_n 5")


def test_command_errors_are_shaped_not_raised(tmp_path, monkeypatch):
    target = plugin(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("status exploded")

    monkeypatch.setattr(CoachPlugin, "_status_text", boom)
    out = target.command("status")
    assert out == "Coach: status failed (RuntimeError)."


def _raise_prune(self):
    raise OSError("disk")


def test_on_session_end_prunes_and_never_raises(tmp_path, monkeypatch):
    target = plugin(tmp_path)
    target.store = Store(tmp_path / "sessions", max_sessions=1)
    target.store.save(target.store.load("s1"))
    target.store.save(target.store.load("s2"))
    target.on_session_end()
    assert len(target.store.sessions_on_disk()) == 1

    monkeypatch.setattr(Store, "prune", _raise_prune)
    assert target.on_session_end() is None


def test_request_text_prefers_history_and_request_fields():
    assert _request_text({}) == ""
    assert (
        _request_text(
            {
                "request": "from request",
                "last_user_message": "from last",
                "conversation_history": [{"role": "user", "content": "from history"}],
            }
        )
        == "from request"
    )
    assert (
        _request_text(
            {
                "last_user_message": "from last",
                "conversation_history": [{"role": "user", "content": "from history"}],
            }
        )
        == "from last"
    )


def test_payload_child_and_tool_helpers():
    assert _payload({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert _child_id({"child_session_id": "child-9"}) == "child-9"
    assert _child_id({"unrelated": "x"}) == ""
    assert _as_turn("3") == 3
    assert _as_turn(object()) == 0
    assert _tool_arg({"command": "git status"}) == "git status"
    assert _tool_arg({"other": "  value  "}) == "value"
    assert _tool_arg({}) == ""
    assert _result_text(None) == ""
    assert json.loads(_result_text({"ok": True})) == {"ok": True}


def test_turn_bookkeeping_helpers():
    state = Store(Path("/tmp")).load("unused")
    _record_turn(state, {"turn_id": "t1", "user": "u", "assistant": "a", "tools": []})
    _record_turn(
        state,
        {
            "turn_id": "t1",
            "user": "u2",
            "assistant": "a2",
            "tools": [{"name": "read_file"}],
        },
    )
    assert len(state.ledger) == 1
    assert _find_turn(state, "missing") is None
    assert _turns_since_review(state) == 1
    state.ledger[0]["reviewed"] = True
    state.ledger[0]["digest"] = "digest"
    assert _turns_since_review(state) == 0
    assert _last_reviewed_digest(state) == "digest"


def test_remember_primary_ignores_empty_models(tmp_path):
    target = CoachPlugin(
        llm=ConsultLlm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
        primary_model="orig",
        primary_provider="p",
    )
    target._remember_primary({"session_id": "s"})
    assert target.primary_model == "orig"
    assert target.coach.primary_model == "orig"


def test_surface_kind_handles_objects_and_failures(tmp_path):
    target = plugin(tmp_path)
    target.surface = SimpleNamespace(value="TELEGRAM")
    assert target._surface_kind() == "telegram"

    def boom():
        raise RuntimeError("no surface")

    target.surface = boom
    assert target._surface_kind() == "cli"


def test_build_plugin_falls_back_when_config_reads_fail(tmp_path):
    class Ctx:
        def get_config(self, key, default):
            raise RuntimeError("config unavailable")

        llm = ConsultLlm()
        session_surface = staticmethod(lambda: "cli")
        session_cwd = staticmethod(lambda: os.getcwd())
        set_config = None

    built = build_plugin(Ctx())
    try:
        assert built.settings == load_settings({})
    finally:
        built.shutdown()


def test_primary_identity_falls_back_to_empty_strings(monkeypatch):
    _hermes_config(
        monkeypatch,
        load_config_readonly=lambda: (_ for _ in ()).throw(
            RuntimeError("config unavailable")
        ),
    )
    assert _primary_identity() == ("", "")


def test_grant_failure_names_both_flags(tmp_path, monkeypatch):
    _hermes_config(
        monkeypatch,
        load_config=lambda: (_ for _ in ()).throw(RuntimeError("locked")),
        save_config=lambda config: (_ for _ in ()).throw(
            RuntimeError("locked")
        ),
    )
    out = plugin(tmp_path).command("grant")
    assert "Could not grant" in out
    assert "allow_provider_override" in out
    assert "allow_model_override" in out


def test_decide_validation_rejects_bad_inputs(tmp_path, monkeypatch):
    from coach.jev import Decision

    target = decide_plugin(tmp_path)
    monkeypatch.setattr(
        "coach.plugin.decide",
        lambda **kwargs: Decision("a", 0.8, {"a": 0.8}, "act", "ok", 0.0),
    )
    assert json.loads(target.tool_decide({}))["error"] == "question_required"
    assert (
        json.loads(target.tool_decide({"question": "x" * 4001, "options": ["a", "b"]}))[
            "error"
        ]
        == "question_too_long"
    )
    assert (
        json.loads(target.tool_decide({"question": "q"}))["error"]
        == "options_required"
    )


def test_review_paths_stay_fail_open(tmp_path):
    target = plugin(tmp_path)

    def fail(**kwargs):
        raise RuntimeError("review exploded")

    target.review_now = fail
    target.collect_evidence = lambda cwd, paths, base="": "diff"
    assert (
        target.on_pre_verify(
            session_id="s",
            turn_id="t1",
            final_response="done",
            changed_paths=["a.py"],
        )
        is None
    )


def test_run_review_skips_paused_sessions_without_calling(tmp_path):
    calls = []

    class QuietStructured(StructuredLlm):
        def complete_structured(self, **kwargs):
            calls.append(kwargs)
            return super().complete_structured(**kwargs)

    target = plugin(tmp_path, review_enabled=True)
    target.llm = QuietStructured(
        {
            "severity": "concern",
            "title": "t",
            "body": "b",
            "evidence": "e",
            "fix": "f",
        }
    )
    target.coach = Coach(
        target.llm, target.settings, primary_model="", primary_provider=""
    )
    target._paused = True
    target.run_review("s", "t1")
    assert calls == []


def test_review_error_is_recorded_without_a_finding(tmp_path):
    class Boom:
        def complete_structured(self, **kwargs):
            raise RuntimeError("provider down")

    target = plugin(tmp_path, review_enabled=True)
    target.coach = Coach(
        Boom(), target.settings, primary_model="", primary_provider=""
    )
    assert (
        target.review_now(
            session_id="s",
            turn_id="t1",
            request="r",
            claim="c",
            evidence="e",
        )
        is None
    )
    assert target.store.load("s").last_error.startswith("provider_error")


def test_jev_body_prefers_named_questions_and_safe_probabilities():
    from coach.jev import _body, _inner, _probabilities

    payload = {"choice": {"choice": "a"}, "other": {"choice": "b"}}
    assert _inner(payload) == {"choice": "a"}
    assert _inner({"answers": {"choice": "b"}}) == {"choice": "b"}
    assert _inner({"answers": [1, 2]}) == {}
    assert _probabilities({"probabilities": {"a": "high"}}, {}) == {"a": 0.0}
    body = _body("q", ["a", "b"], {"goal": "x"}, model="m")
    assert body["model"] == "m"
    assert body["questions"]["choice"]["criteria"] == {"a": "a", "b": "b"}


def test_jev_rejects_malformed_payloads_and_unknown_backends():
    from coach.jev import decide

    class Resp:
        def __init__(self, text):
            self.text = text

        def read(self):
            return self.text.encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    bad = decide(
        backend="jev-openrouter",
        question="q",
        options=["a", "b"],
        state={},
        api_key="k",
        http=lambda req, **kwargs: Resp("[1, 2]"),
    )
    assert bad.gate == "error"
    assert (
        decide(
            backend="unknown-backend",
            question="q",
            options=["a", "b"],
            state={},
            api_key="k",
        ).gate
        == "error"
    )


def test_chat_model_failure_modes_stay_errors():
    from types import SimpleNamespace

    from coach.jev import decide

    class Boom:
        def ask(self, *args, **kwargs):
            raise RuntimeError("down")

    assert (
        decide(
            backend="chat-model",
            question="q",
            options=["a", "b"],
            state={},
            chat=None,
        ).gate
        == "error"
    )
    assert (
        decide(
            backend="chat-model",
            question="q",
            options=["a", "b"],
            state={},
            chat=Boom(),
        ).gate
        == "error"
    )
    assert (
        decide(
            backend="chat-model",
            question="q",
            options=["a", "b"],
            state={},
            chat=SimpleNamespace(
                parsed={}, cost_usd=0.0, error="boom"
            ),
        ).gate
        == "error"
    )


def test_settings_coercion_helpers():

    assert _as_bool(1, False) is False
    assert _as_bool("on", False) is True
    assert _bool_arg("enable") is True
    assert _bool_arg("maybe") is None
    assert load_settings({"active_every_n": True}).active_every_n == DEFAULTS[
        "active_every_n"
    ]
    assert load_settings({"active_every_n": 2.5}).active_every_n == DEFAULTS[
        "active_every_n"
    ]
    assert load_settings({"decide_backend": "nope"}).decide_backend == DEFAULTS[
        "decide_backend"
    ]
    assert load_settings({"decide_model": 42}).decide_model == DEFAULTS["decide_model"]


def test_state_coercion_and_store_helpers(tmp_path):
    assert state_int(True) == 0
    assert state_float(float("inf"), 2.0) == 2.0
    assert _dict_items([{}, [], "x"]) == [{}]

    store = Store(tmp_path)
    first = store.load("s1")
    first.spend_usd = 0.25
    store.save(first)
    second = store.load("s2")
    second.spend_usd = 0.75
    store.save(second)
    assert store.spend_total() == 1.0
    assert store.sessions_on_disk() == ["s1", "s2"]
    store.forget("s1")
    assert store.has("s1") is False
    store.forget("missing")

    corrupt = store._path("corrupt")
    corrupt.write_text("[]", encoding="utf-8")
    assert store.load("corrupt").last_error == "state reset"


def test_ledger_helpers_and_bounds():
    assert _summary_line(1) == "- 1 tool call"
    assert _summary_line(3) == "- 3 tool calls"
    state = Store(Path("/tmp")).load("unused")
    state.ledger = [{"not": "a turn"}, {"tools": "not-a-list"}]
    messages = build_messages(
        system="",
        guidance="",
        state=state,
        question=None,
        max_tokens=100,
        tool_result_chars=10,
        recent_turns=0,
    )
    assert any(message["role"] == "user" for message in messages)
    assert tool_card("read", "a", "ok", 10, "preview", max_chars=-5)["preview"] == ""


def test_findings_reject_non_object_fenced_payloads():
    assert parse_finding(None, '```json\n[1, 2]\n```') is None
    assert parse_finding({"severity": "NONE"}) is None


def test_evidence_helpers_handle_unreadable_inputs(tmp_path):
    assert _read_text(tmp_path / "missing.txt") is None
    assert (
        _git(
            lambda argv, **kwargs: SimpleNamespace(),
            tmp_path,
            ["git", "status"],
        )
        is None
    )
    assert _stdout(SimpleNamespace(stdout=b"a")) == "a"
    assert _safe_paths(tmp_path, ["", "  "]) == []
    assert _heads(tmp_path, ["missing.txt"], lambda path: None) == ""


def test_dashboard_config_shape_and_secret_flag(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    body = client.get("/config").json()
    assert set(body["values"]) == set(DEFAULTS)
    assert body["values"]["decide_typesafe_api_key"] == ""
    assert "decide_typesafe_api_key" not in body["defaults"]
    assert body["choices"]["delivery_policy"] == [
        "severity",
        "ambient",
        "corrective",
    ]
    assert body["secret_set"] is False


def test_dashboard_status_aggregates_sessions(tmp_path):
    store = Store(tmp_path / "coach" / "sessions")
    first = store.load("one")
    first.spend_usd = 0.2
    first.pending = [{"text": "note"}]
    first.last_error = "boom"
    store.save(first)
    second = store.load("two")
    second.spend_usd = 0.3
    store.save(second)
    body = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1").get("/status").json()
    assert body["spend_usd"] == 0.5
    assert body["pending_notes"] == 1
    assert body["sessions"] == 2
    assert body["last_error"] == "boom"


def test_dashboard_config_write_failure_is_a_500(tmp_path, monkeypatch):
    app = app_for_tests(home=tmp_path)
    app.state.config = None
    monkeypatch.setattr(api, "_save_host_config", lambda config: False)
    response = TestClient(app, base_url="http://127.0.0.1").put("/config", json={"values": {"active_enabled": True}})
    assert response.status_code == 500


def test_dashboard_grant_write_failure_is_a_500(tmp_path, monkeypatch):
    app = app_for_tests(home=tmp_path)
    app.state.config = None
    monkeypatch.setattr(api, "_save_host_config", lambda config: False)
    response = TestClient(app, base_url="http://127.0.0.1").post("/permissions/grant", json={})
    assert response.status_code == 500


def test_dashboard_host_config_is_tolerant(monkeypatch):
    """Load failure is None (write refusal), not an empty dict to persist."""
    _hermes_config_test(monkeypatch, load_config=lambda: ["not", "a", "mapping"])
    assert api._load_host_config() is None
    _hermes_config_test(
        monkeypatch,
        load_config=lambda: (_ for _ in ()).throw(RuntimeError("locked")),
    )
    assert api._load_host_config() is None


def test_dashboard_transient_read_failure_never_persists(tmp_path, monkeypatch):
    """A failed load raises config_read_failed instead of writing a partial config."""
    app = app_for_tests(home=tmp_path)
    app.state.config = None
    saved: list = []
    monkeypatch.setattr(api, "_save_host_config", lambda config: saved.append(config) or True)
    _hermes_config_test(
        monkeypatch,
        load_config=lambda: (_ for _ in ()).throw(RuntimeError("locked")),
    )
    response = TestClient(app, base_url="http://127.0.0.1").post("/permissions/grant", json={})
    assert response.status_code == 500
    assert response.json()["detail"] == "config_read_failed"
    assert saved == []


def test_dashboard_failed_read_marker_survives(monkeypatch):
    """The host's FailedConfigRead marker must reach save_config intact."""
    saved: list = []

    class FailedConfigRead(dict):
        pass

    _hermes_config_test(
        monkeypatch,
        load_config=lambda: FailedConfigRead({"kept": True}),
        save_config=lambda config: saved.append(config),
    )
    loaded = api._load_host_config()
    assert loaded is not None
    api._apply_grant(loaded)
    assert api._save_host_config(loaded)
    assert type(saved[-1]).__name__ == "FailedConfigRead"
    assert saved[-1]["kept"] is True


def _hermes_config_test(monkeypatch, **functions):
    parent = types.ModuleType("hermes_cli")
    parent.__path__ = []
    module = types.ModuleType("hermes_cli.config")
    for name, function in functions.items():
        setattr(module, name, function)
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    monkeypatch.setattr(parent, "config", module, raising=False)
    return module


def test_dashboard_paths_and_guidance_helpers(tmp_path):
    assert api._mapping_section({"plugins": []}, "plugins") == {}
    assert api._json_value({"choice": None, "items": [1, None]}) == {
        "choice": None,
        "items": [1, None],
    }
    assert api._home(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(home=tmp_path)))) == tmp_path
    assert api._clean_scope("cwd") == "project"
    with pytest.raises(HTTPException):
        api._clean_scope("everywhere")
    assert api._read_guidance(tmp_path / "missing.md") == ""
    with pytest.raises(HTTPException):
        api._project_dir(tmp_path, "")
    with pytest.raises(HTTPException):
        api._project_dir(tmp_path, str(tmp_path / "missing"))
    outside = tmp_path.parent / "outside-coach-project"
    outside.mkdir(exist_ok=True)
    with pytest.raises(HTTPException):
        api._project_dir(tmp_path, str(outside))


def test_worker_drain_times_out_while_busy():
    from coach.worker import Worker

    started = threading.Event()
    release = threading.Event()

    def run(session_id, turn_id):
        started.set()
        assert release.wait(2.0)

    worker = Worker(run)
    try:
        assert worker.submit("s", "t") is True
        assert started.wait(2.0) is True
        assert worker.drain(0.01) is False
        release.set()
        assert worker.drain(2.0) is True
        assert worker.submit("") is False
    finally:
        release.set()
        worker.stop()


def test_worker_stop_is_safe_without_a_submission():
    from coach.worker import Worker

    worker = Worker(lambda session_id, turn_id: None)
    worker.stop()
    assert worker.submit("s", "t") is False


def test_worker_does_not_queue_the_running_turn_again():
    """post_llm_call fires once per model call: re-submitting the in-flight
    turn must not review (and pay for) it a second time."""
    from coach.worker import Worker

    calls = []
    started = threading.Event()
    release = threading.Event()

    def run(session_id, turn_id):
        calls.append((session_id, turn_id))
        started.set()
        release.wait(2.0)

    worker = Worker(run)
    try:
        assert worker.submit("s", "t") is True
        assert started.wait(2.0) is True
        assert worker.submit("s", "t") is True
        release.set()
        assert worker.drain(2.0) is True
        assert calls == [("s", "t")]
    finally:
        release.set()
        worker.stop()


def test_trim_log_keeps_small_files_and_log_names_are_safe(tmp_path):

    target = tmp_path / "coach-coverage-test.jsonl"
    target.write_text("a\nb\n", encoding="utf-8")
    try:
        from coach.plugin import _trim_log

        _trim_log(target, keep=10)
        assert target.read_text(encoding="utf-8").splitlines() == ["a", "b"]
    finally:
        target.unlink(missing_ok=True)
    assert _log_name("") == "session"
    assert _log_name("task/1") != _log_name("task_1")
    assert _log_name("task/1") == _log_name("task/1")
    assert "/" not in _log_name("../../etc/passwd")




def test_decide_error_counts_an_unpriced_attempt(tmp_path, monkeypatch):
    """A failed decide call writes nothing to the bill; the counter and
    /coach status must surface it instead of silently under-counting."""
    from coach.jev import Decision

    target = decide_plugin(tmp_path)
    monkeypatch.setattr(
        "coach.plugin.decide",
        lambda **kw: Decision(
            decision="",
            confidence=0.0,
            probabilities={},
            gate="error",
            rationale="jev-openrouter call failed: URLError: outage",
            cost_usd=0.0,
        ),
    )
    out = json.loads(
        target.tool_decide({"question": "Choose?", "options": ["a", "b"]})
    )
    assert "URLError" in out["error"]
    state = target.store.load("current")
    assert state.calls.get("decide_unpriced") == 1
    assert state.last_error.startswith("jev-openrouter call failed")
    assert target.status()["decide_unpriced"] == 1
