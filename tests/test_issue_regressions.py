# Cross-cutting regression tests for issues found while building the plugin.
#
# Each test pins one behavior that broke, misled, or silently degraded the
# plugin during development or live verification. Unit tests next to each
# module cover the happy paths; these tests cover the failure modes.

import copy
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi.testclient import TestClient

from coach.advisor import Coach
from coach.delivery import format_card, route
from coach.evidence import collect
from coach.findings import Finding, is_deliverable, parse_finding
from coach.jev import Decision, decide
from coach.ledger import append_turn, build_messages, tool_card
from coach.plugin import (
    ASK_DESCRIPTION,
    ASK_SCHEMA,
    DECIDE_DESCRIPTION,
    DECIDE_SCHEMA,
    CoachPlugin,
    _coerce_setting,
    _parse_model,
    _primary_identity,
    build_plugin,
)
from coach.settings import DEFAULTS, load_settings
from coach.state import Store
from dashboard.plugin_api import app_for_tests

REPO = Path(__file__).resolve().parent.parent


def _product_text(relative_path: str) -> str:
    return (REPO / relative_path).read_text(encoding="utf-8")


class _ConsultLlm:
    def __init__(self, text="Advice body.", provider="openrouter", model="x"):
        self.text = text
        self.provider = provider
        self.model = model
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0
        )
        return SimpleNamespace(
            text=self.text,
            provider=self.provider,
            model=self.model,
            usage=usage,
            audit={},
        )

    def complete_structured(self, **kwargs):
        raise AssertionError("consult must not use structured output")


class _StructuredLlmForAdvisor:
    def __init__(self):
        self.calls = []

    def complete(self, messages, **kwargs):
        raise AssertionError("structured test must not use plain completion")

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            text="ok",
            provider=kwargs.get("provider", "p"),
            model=kwargs.get("model", "m"),
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
            parsed={"severity": "none"},
            audit={},
        )


class _StructuredLlm:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        result = SimpleNamespace(text="", usage=None, parsed=None)
        result.parsed = self.payload
        result.model = kwargs["model"]
        result.provider = kwargs["provider"]
        return result


class _FakeWorker:
    def __init__(self):
        self.submitted = []

    def submit(self, session_id, turn_id=""):
        self.submitted.append((session_id, turn_id))
        return True


def _plugin(tmp_path, **config):
    base = {"coach_model": {"provider": "openrouter", "model": "x"}}
    base.update(config)
    return CoachPlugin(
        llm=_ConsultLlm(), store=Store(tmp_path), settings=load_settings(base)
    )


# ---------------------------------------------------------------------------
# Packaging and host contract
# ---------------------------------------------------------------------------


def test_plugin_package_uses_relative_imports_only():
    """The host mounts this package synthetically, so absolute imports fail."""
    bad = []
    candidates = [REPO / "__init__.py", *sorted((REPO / "coach").glob("*.py"))]
    for path in candidates:
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            stripped = line.strip()
            if (
                stripped.startswith("from coach.")
                or stripped.startswith("from coach import")
                or stripped.startswith("import coach.")
            ):
                bad.append(f"{path.name}:{number}:{stripped}")
    assert not bad, bad


def test_root_register_imports_the_package_lazily():
    """Importing the loader entry must not import the package at module scope."""
    text = _product_text("__init__.py")
    head, _, body = text.partition("def register")
    assert "from .coach.plugin import" not in head
    assert "from .coach.plugin import" in body


def test_register_wires_tools_hooks_command_and_shutdown():
    text = _product_text("__init__.py")
    for token in (
        "coach_ask",
        "coach_decide",
        "pre_verify",
        "post_tool_call",
        "post_llm_call",
        "pre_llm_call",
        "subagent_start",
        "subagent_stop",
        "on_session_end",
        "register_command",
        "on_unload(plugin.shutdown)",
    ):
        assert token in text, token


def test_manifest_contract_matches_the_live_plugin():
    manifest = yaml.safe_load((REPO / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "coach"
    assert manifest["kind"] == "standalone"
    # No config_schema: a schema would make the host render a second,
    # duplicate "Agent settings" tab beside the Coach page.
    assert not manifest.get("config_schema")
    assert set(manifest["provides_tools"]) == {"coach_ask", "coach_decide"}
    assert set(manifest["provides_hooks"]) == {
        "pre_verify",
        "post_tool_call",
        "post_llm_call",
        "pre_llm_call",
        "subagent_start",
        "subagent_stop",
        "on_session_end",
    }


def test_tool_schemas_and_descriptions_are_host_ready():
    assert ASK_SCHEMA["required"] == ["question"]
    assert ASK_SCHEMA["additionalProperties"] is False
    assert ASK_SCHEMA["properties"]["question"]["maxLength"] == 4000
    assert DECIDE_SCHEMA["required"] == ["question", "options"]
    assert DECIDE_SCHEMA["additionalProperties"] is False
    assert "Relay" in ASK_DESCRIPTION and "attributed" in ASK_DESCRIPTION
    assert "act" in DECIDE_DESCRIPTION and "escalate" in DECIDE_DESCRIPTION


def test_test_script_runs_both_gates():
    text = _product_text("scripts/test.sh")
    assert "pytest tests" in text
    assert "check-desktop-page.mjs" in text
    assert "pipefail" in text


# ---------------------------------------------------------------------------
# Configuration and settings validation
# ---------------------------------------------------------------------------


def test_guidance_lives_in_files_not_settings_keys():
    assert [key for key in DEFAULTS if key.startswith("guidance_")] == [
        "guidance_enabled"
    ]


def test_settings_reject_non_finite_and_negative_numbers():
    settings = load_settings(
        {
            "budget_session_usd": float("nan"),
            "ledger_max_tokens": float("inf"),
            "ledger_max_tool_result_chars": -10,
            "decide_min_confidence": 2,
        }
    )
    assert settings.budget_session_usd == DEFAULTS["budget_session_usd"]
    assert settings.ledger_max_tokens >= 500
    assert settings.ledger_max_tool_result_chars >= 50
    assert settings.decide_min_confidence == 1.0


def test_model_ids_with_slashes_survive_settings():
    settings = load_settings(
        {"coach_model": {"provider": "openrouter", "model": "a/b/c"}}
    )
    assert settings.coach_model.model == "a/b/c"
    assert settings.model_for("consult").model == "a/b/c"


def test_unknown_tool_mode_has_no_model():
    settings = load_settings({"coach_model": {"provider": "p", "model": "m"}})
    assert settings.model_for("bogus") is None


# ---------------------------------------------------------------------------
# State, advice history, and logs
# ---------------------------------------------------------------------------


def test_advice_ring_keeps_only_the_newest_entries(tmp_path):
    plugin = _plugin(tmp_path)
    for index in range(6):
        plugin.tool_ask(
            {"question": f"question {index}", "turn_id": f"t{index}"},
            session_id="s",
        )
    advice = plugin.store.load("s").advice
    assert [row["question"] for row in advice] == [f"question {index}" for index in range(1, 6)]


def test_last_advice_never_leaks_across_known_sessions(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.tool_ask({"question": "only question"}, session_id="s1")
    plugin.store.save(plugin.store.load("s2"))
    out = plugin.command("last", session_id="s2")
    assert "No coach advice" in out
    assert "(from session" not in out


def test_log_entries_carry_kind_timestamp_and_evidence(tmp_path):
    from coach.plugin import Finding as PluginFinding

    plugin = CoachPlugin(
        llm=_ConsultLlm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
    )
    plugin._log_review(
        "s1",
        "t1",
        SimpleNamespace(
            finding=PluginFinding(
                "concern", "Title", "Body", "a.py:1", "Fix it."
            ),
            usage=None,
            cost_usd=0.0,
            provider="p",
            model="m",
            error="",
        ),
    )
    record = json.loads(
        (tmp_path / "logs" / "s1.jsonl").read_text(encoding="utf-8").strip()
    )
    assert record["kind"] == "review"
    assert record["severity"] == "concern"
    assert record["body"] == "Body"
    assert record["evidence"] == "a.py:1"
    assert record["fix"] == "Fix it."
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", record["at"])


def test_session_filenames_do_not_collide(tmp_path):
    store = Store(tmp_path)
    left = store._path("team/chat")
    right = store._path("team_chat")
    assert left.name != right.name
    store.save(store.load("team/chat"))
    store.save(store.load("team_chat"))
    assert set(store.sessions_on_disk()) == {"team/chat", "team_chat"}


def test_prune_orders_by_file_mtime_not_content(tmp_path):
    store = Store(tmp_path, max_sessions=1)
    old_path = store._path("old-session")
    new_path = store._path("new-session")
    old_path.write_text(
        json.dumps({"session_id": "old-session", "updated_at": 9999}),
        encoding="utf-8",
    )
    new_path.write_text(
        json.dumps({"session_id": "new-session", "updated_at": 1}),
        encoding="utf-8",
    )
    os.utime(old_path, (100, 100))
    os.utime(new_path, (200, 200))
    store.prune()
    assert store.sessions_on_disk() == ["new-session"]


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def test_fixed_prompt_parts_reduce_the_ledger_budget():
    state = Store(Path("/tmp")).load("unused")
    append_turn(
        state,
        turn_id="t1",
        user="u",
        assistant="a",
        tools=[tool_card("read_file", "a.py", "ok", 5000, "x" * 5000, max_chars=400)],
    )
    roomy = build_messages(
        system="",
        guidance="",
        state=state,
        question=None,
        max_tokens=600,
        tool_result_chars=400,
        recent_turns=1,
    )
    crowded = build_messages(
        system="S" * 800,
        guidance="G" * 800,
        state=state,
        question="Q" * 800,
        max_tokens=600,
        tool_result_chars=400,
        recent_turns=1,
    )
    roomy_text = "\n".join(message["content"] for message in roomy)
    crowded_text = "\n".join(message["content"] for message in crowded)
    assert "x" * 100 in roomy_text
    assert "x" * 100 not in crowded_text


def test_an_oversized_user_message_is_truncated_not_dropped():
    state = Store(Path("/tmp")).load("unused")
    append_turn(state, turn_id="t1", user="U" * 10_000, assistant="a", tools=[])
    messages = build_messages(
        system="",
        guidance="",
        state=state,
        question=None,
        max_tokens=100,
        tool_result_chars=400,
        recent_turns=1,
    )
    body = messages[1]["content"]
    assert body.startswith("user: U")
    assert len(body) <= 400


def test_ledger_keeps_at_most_sixty_turns():
    state = Store(Path("/tmp")).load("unused")
    for index in range(65):
        append_turn(
            state, turn_id=f"t{index}", user=f"u{index}", assistant="a", tools=[]
        )
    assert len(state.ledger) == 60
    assert state.ledger[0]["turn_id"] == "t5"
    assert state.ledger[-1]["turn_id"] == "t64"


# ---------------------------------------------------------------------------
# Findings, delivery, and evidence
# ---------------------------------------------------------------------------


def test_findings_accept_fenced_json_and_reject_severity_games():
    payload = {
        "severity": "concern",
        "title": "  Title  ",
        "body": "Body",
        "evidence": "a.py:1",
        "fix": "Fix.",
    }
    text = "```json\n" + json.dumps(payload) + "\n```"
    finding = parse_finding(None, text)
    assert finding is not None and finding.severity == "concern"
    assert parse_finding({"severity": "CRITICAL", "title": "t", "body": "b"}) is None
    assert is_deliverable(Finding("nit", "t", "b", "e", "f")) is True


def test_delivery_card_points_at_the_durable_record():
    card = format_card(
        Finding("concern", "Title", "Body", "a.py:1", "Fix it.")
    )
    assert "/coach log" in card
    assert "Status" in card


def test_evidence_uses_nul_separated_unquoted_git_status(tmp_path):
    calls = []
    (tmp_path / "a file.py").write_text("print('hi')\n", encoding="utf-8")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "status" in argv:
            import subprocess

            return subprocess.CompletedProcess(argv, 0, "?? a file.py\0", "")
        import subprocess

        return subprocess.CompletedProcess(argv, 0, "", "")

    text = collect(str(tmp_path), ["a file.py"], run=fake_run)
    status_argv, status_kwargs = calls[0]
    assert status_argv[:4] == ["git", "-c", "core.quotePath=false", "status"]
    assert "--porcelain" in status_argv and "-z" in status_argv
    assert status_kwargs["timeout"] == 5
    assert status_kwargs["shell"] is False
    assert "(new file)" in text


def test_review_skips_unreadable_changes_instead_of_reviewing_air(tmp_path):
    from tests.test_review_gate import PAY, _p

    plugin = _p(tmp_path)
    plugin.collect_evidence = lambda cwd, paths: "   "
    plugin.review_now = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("empty evidence must not reach the model")
    )
    assert plugin.on_pre_verify(**PAY) is None


# ---------------------------------------------------------------------------
# Models, timeouts, budgets, and host identity
# ---------------------------------------------------------------------------


def test_structured_calls_carry_system_prompt_instructions_and_schema():
    llm = _StructuredLlmForAdvisor()
    coach = Coach(llm, load_settings({"coach_model": {"provider": "p", "model": "m"}}))
    coach.ask(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "body"}],
        mode="consult",
        structured=True,
        schema={"type": "object"},
        instructions="Do X",
        timeout=9,
    )
    call = llm.calls[0]
    assert call["system_prompt"] == "sys"
    assert call["instructions"] == "Do X"
    assert call["json_schema"] == {"type": "object"}
    assert call["timeout"] == 9


def test_consult_uses_the_configured_worker_timeout(tmp_path):
    plugin = _plugin(
        tmp_path,
        worker_timeout_s=7,
    )
    plugin.tool_ask({"question": "q"})
    assert plugin.llm.calls[0]["timeout"] == 7.0


def test_warn_budget_does_not_stop_automatic_modes(tmp_path):
    plugin = _plugin(
        tmp_path,
        budget_session_usd=0.01,
        budget_on_exceed="warn",
        active_enabled=True,
    )
    state = plugin.store.load("s")
    state.spend_usd = 1.0
    plugin.store.save(state)
    assert (
        plugin.should_review(
            state,
            {"turn_id": "t1", "tools": [{"name": "read_file"}]},
            plugin.settings,
        )
        is True
    )
    assert json.loads(plugin.tool_ask({"question": "q"}, session_id="s"))["ok"] is True


def test_hard_budget_stops_decide_tool_calls(tmp_path, monkeypatch):
    from tests.test_plugin_decide import ARGS, _fake, _p

    plugin = _p(tmp_path, budget_session_usd=0.01, budget_on_exceed="hard")
    state = plugin.store.load("current")
    state.spend_usd = 1.0
    plugin.store.save(state)
    monkeypatch.setattr("coach.plugin.decide", _fake())
    assert json.loads(plugin.tool_decide(ARGS))["error"] == "budget_exhausted"


def test_decide_merges_dispatcher_turn_and_session_ids(tmp_path, monkeypatch):
    from coach.jev import Decision
    from tests.test_plugin_decide import _p

    plugin = _p(tmp_path)
    monkeypatch.setattr(
        "coach.plugin.decide",
        lambda **kwargs: Decision("a", 0.8, {"a": 0.8}, "act", "ok", 0.0),
    )
    out = json.loads(
        plugin.tool_decide(
            {"question": "q", "options": ["a", "b"]},
            session_id="s1",
            task_id="task-9",
        )
    )
    assert out["ok"] is True
    assert plugin.store.load("s1").calls["decide:task-9"] == 1


def test_decide_rejects_duplicate_or_blank_options(tmp_path, monkeypatch):
    from tests.test_plugin_decide import _fake, _p

    plugin = _p(tmp_path)
    monkeypatch.setattr("coach.plugin.decide", _fake())
    assert (
        json.loads(
            plugin.tool_decide({"question": "q", "options": ["a", " a "]})
        )["error"]
        == "options_invalid"
    )


def test_jev_request_envelope_carries_model_state_and_auth():
    from coach.jev import decide

    seen = {}

    class Resp:
        def read(self):
            return json.dumps(
                {
                    "answers": {"choice": {"type": "choice", "choice": "a"}},
                    "verdicts": {
                        "choice": {"gate": "act", "confidence": 0.8}
                    },
                    "usage": {},
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_http(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return Resp()

    decision = decide(
        backend="jev-openrouter",
        question="q",
        options=["a", "b"],
        state={"goal": "x"},
        api_key="k",
        http=fake_http,
    )
    assert decision.decision == "a"
    assert seen["url"].endswith("/api/alpha/decisions")
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer k"
    assert seen["timeout"] == 15
    assert seen["body"]["model"] == "typesafe/jev-1.14"
    assert seen["body"]["state"] == {"goal": "x"}
    assert set(seen["body"]["questions"]["choice"]["criteria"]) == {"a", "b"}


def test_jev_treats_non_numeric_confidence_as_no_evidence():
    from coach.jev import decide

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    body = {
        "answers": {"choice": {"choice": "a"}},
        "verdicts": {"choice": {"gate": "act", "confidence": "high"}},
        "usage": {},
    }
    decision = decide(
        backend="jev-openrouter",
        question="q",
        options=["a", "b"],
        state={},
        api_key="k",
        http=lambda req, **kw: Resp(body),
    )
    assert decision.gate == "escalate"


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

    assert "Granted" in _plugin(tmp_path).command("grant")
    llm = saved["plugins"]["entries"]["coach"]["llm"]
    assert llm == {"allow_provider_override": True, "allow_model_override": True}


def test_primary_identity_prefers_the_live_hook_model(monkeypatch):
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


def test_build_plugin_reads_the_session_working_directory(tmp_path):
    llm = _ConsultLlm()
    ctx = SimpleNamespace(
        get_config=lambda key, default: default,
        llm=llm,
        model="",
        model_name="",
        provider="",
        model_provider="",
        post_message=None,
        inject_message=None,
        session_surface=lambda: "desktop",
        session_cwd=lambda: "/proj",
        set_config=None,
    )
    plugin = build_plugin(ctx)
    try:
        assert plugin.cwd() == "/proj"
    finally:
        plugin.shutdown()


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


# ---------------------------------------------------------------------------
# Dashboard API guards
# ---------------------------------------------------------------------------


def test_dashboard_config_rejects_secret_empty_and_malformed_values(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    assert (
        client.put(
            "/config", json={"values": {"decide_typesafe_api_key": "x"}}
        ).status_code
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
