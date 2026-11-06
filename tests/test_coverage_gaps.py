# Coverage for small helpers, validation edges, and host-failure branches.
#
# These tests intentionally target pure helpers and failure paths rather than
# repeating the main behavioral suites.

import copy
import json
import os
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from coach.advisor import Coach
from coach.delivery import format_ambient
from coach.evidence import _git, _heads, _read_text, _safe_paths, _stdout, collect
from coach.findings import Finding, is_deliverable, parse_finding
from coach.ledger import _summary_line, append_turn, build_messages, tool_card
from coach.plugin import (
    ASK_SCHEMA,
    CoachPlugin,
    _as_turn,
    _bool_arg,
    _child_id,
    _coerce_setting,
    _find_turn,
    _last_reviewed_digest,
    _log_name,
    _parse_model,
    _payload,
    _primary_identity,
    _record_turn,
    _request_text,
    _result_text,
    _tool_arg,
    _trim_log,
    _turns_since_review,
    build_plugin,
)
from coach.settings import (
    DEFAULTS,
    _as_bool,
    _as_choice,
    _as_confidence,
    _as_float,
    _as_int,
    _as_model,
    _as_text,
    load_settings,
)
from coach.state import (
    SessionState,
    Store,
    _as_float as state_float,
    _as_int as state_int,
    _calls,
    _dict_items,
    _safe_name,
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
    coach_plugin.guidance.write("global", "", "Be blunt.")
    coach_plugin.guidance.write("project", str(project), "Use tabs.")
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
    target = review_plugin(tmp_path) if False else plugin(tmp_path)

    def fail(**kwargs):
        raise RuntimeError("review exploded")

    target.review_now = fail
    target.collect_evidence = lambda cwd, paths: "diff"
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
    from coach.jev import decide
    from types import SimpleNamespace

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
    from coach.plugin import _bool_arg

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
    client = TestClient(app_for_tests(home=tmp_path))
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
    body = TestClient(app_for_tests(home=tmp_path)).get("/status").json()
    assert body["spend_usd"] == 0.5
    assert body["pending_notes"] == 1
    assert body["sessions"] == 2
    assert body["last_error"] == "boom"


def test_dashboard_config_write_failure_is_a_500(tmp_path, monkeypatch):
    app = app_for_tests(home=tmp_path)
    app.state.config = None
    monkeypatch.setattr(api, "_save_host_config", lambda config: False)
    response = TestClient(app).put("/config", json={"values": {"active_enabled": True}})
    assert response.status_code == 500


def test_dashboard_grant_write_failure_is_a_500(tmp_path, monkeypatch):
    app = app_for_tests(home=tmp_path)
    app.state.config = None
    monkeypatch.setattr(api, "_save_host_config", lambda config: False)
    response = TestClient(app).post("/permissions/grant", json={})
    assert response.status_code == 500


def test_dashboard_host_config_is_tolerant(monkeypatch):
    _hermes_config_test(monkeypatch, load_config=lambda: ["not", "a", "mapping"])
    assert api._load_host_config() == {}
    _hermes_config_test(
        monkeypatch,
        load_config=lambda: (_ for _ in ()).throw(RuntimeError("locked")),
    )
    assert api._load_host_config() == {}


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
    with pytest.raises(Exception):
        api._clean_scope("everywhere")
    assert api._read_guidance(tmp_path / "missing.md") == ""
    with pytest.raises(Exception):
        api._project_dir(tmp_path, "")
    with pytest.raises(Exception):
        api._project_dir(tmp_path, str(tmp_path / "missing"))
    outside = tmp_path.parent / "outside-coach-project"
    outside.mkdir(exist_ok=True)
    with pytest.raises(Exception):
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


def test_trim_log_keeps_small_files_and_log_names_are_safe():
    from coach.plugin import _log_name

    target = Path("/tmp/coach-coverage-test.jsonl")
    target.write_text("a\nb\n", encoding="utf-8")
    try:
        from coach.plugin import _trim_log

        _trim_log(target, keep=10)
        assert target.read_text(encoding="utf-8").splitlines() == ["a", "b"]
    finally:
        target.unlink(missing_ok=True)
    assert _log_name("") == "session"
    assert "/" not in _log_name("../../etc/passwd")


# ---------------------------------------------------------------------------
# Dashboard endpoint behavior
# ---------------------------------------------------------------------------


def test_dashboard_config_rejects_secret_empty_and_malformed_values(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    assert (
        client.put("/config", json={"values": {"decide_typesafe_api_key": "x"}}).status_code
        == 400
    )
    assert client.put("/config", json={"values": {}}).status_code == 400
    assert client.put("/config", json={"values": ["x"]}).status_code == 400


def test_dashboard_grant_reports_permissions(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    assert client.post("/permissions/grant", json={}).status_code == 200
    config = client.get("/config").json()
    assert config["permissions"]["model_overrides"] is True


def test_dashboard_guidance_rejects_unknown_scope_and_oversize_text(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    assert (
        client.put(
            "/guidance",
            json={"scope": "everywhere", "cwd": "", "text": "x"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/guidance",
            json={"scope": "global", "cwd": "", "text": "x" * 8001},
        ).status_code
        == 400
    )


def test_dashboard_global_guidance_creates_its_parent(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    response = client.put(
        "/guidance", json={"scope": "global", "cwd": "", "text": "Be blunt."}
    )
    assert response.status_code == 200
    assert (tmp_path / "coach" / "guidance.md").read_text(
        encoding="utf-8"
    ) == "Be blunt."


def test_dashboard_recent_orders_and_filters(tmp_path):
    logs = tmp_path / "coach" / "logs"
    logs.mkdir(parents=True)
    (logs / "a.jsonl").write_text(
        '{"at":"2026-01-01T00:00:00+00:00","title":"old","body":"b",'
        '"severity":"nit","session_id":"s1"}\n',
        encoding="utf-8",
    )
    (logs / "b.jsonl").write_text(
        '{"at":"2026-02-01T00:00:00+00:00","title":"new","body":"b2",'
        '"severity":"concern","session_id":"s2"}\n',
        encoding="utf-8",
    )
    client = TestClient(app_for_tests(home=tmp_path))
    rows = client.get("/recent").json()["reviews"]
    assert [row["title"] for row in rows] == ["new", "old"]
    rows = client.get("/recent", params={"session": "s1"}).json()["reviews"]
    assert [row["title"] for row in rows] == ["old"]


# ---------------------------------------------------------------------------
# Grant, identity, and command model handling
# ---------------------------------------------------------------------------


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
    assert "kept" in out and "(from session" in out


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


def test_guidance_write_truncates_and_names_unknown_scopes(tmp_path):
    from coach.guidance import GUIDANCE_MAX_CHARS, Guidance

    guidance = Guidance(tmp_path)
    message = guidance.write("global", "", "x" * (GUIDANCE_MAX_CHARS + 10))
    assert "limit" in message
    assert len(guidance.read("global", "")) == GUIDANCE_MAX_CHARS
    with pytest.raises(ValueError):
        guidance._path("everywhere", "")
