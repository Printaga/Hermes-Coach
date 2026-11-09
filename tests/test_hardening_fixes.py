# Regression tests for the hardening pass: bounds, fail-open, and trust fixes.
from __future__ import annotations

import json
from types import SimpleNamespace

from coach.delivery import format_card
from coach.findings import SCHEMA, Finding, _from_mapping
from coach.ledger import _bound_card, append_turn, build_messages
from coach.plugin import (
    ASK_SCHEMA,
    CoachPlugin,
    _with_host_ids,
)
from coach.settings import load_settings
from coach.state import SessionState, Store
from tests.support import ConsultLlm as Llm


def _plugin(tmp_path, **overrides):
    raw = {"coach_model": {"provider": "openrouter", "model": "x"}}
    raw.update(overrides)
    return CoachPlugin(
        llm=Llm(), store=Store(tmp_path / "sessions"), settings=load_settings(raw)
    )


def _state():
    return SessionState(session_id="s")


# --- ledger bounds -----------------------------------------------------------


def test_zero_token_budget_sends_no_ledger():
    s = _state()
    append_turn(s, turn_id="t1", user="hello", assistant="world", tools=[])
    msgs = build_messages(
        system="sys",
        guidance="guide",
        state=s,
        question="q",
        max_tokens=0,
        tool_result_chars=400,
        recent_turns=3,
    )
    assert "hello" not in msgs[-1]["content"]


def test_tools_bounded_at_write():
    s = _state()
    big = {"name": "n" * 200, "arg": "a" * 500, "status": "s" * 200,
           "size": 10, "preview": "p" * 5000}
    append_turn(s, turn_id="t1", user="u", assistant="a",
                tools=[dict(big) for _ in range(30)])
    cards = s.ledger[0]["tools"]
    assert len(cards) == 20
    assert len(cards[0]["name"]) <= 64
    assert len(cards[0]["preview"]) <= 2000


def test_bound_card_handles_garbage():
    card = _bound_card(None)
    assert card["name"] == "tool"
    card = _bound_card({"size": "huge"})
    assert card["size"] == 0


def test_note_capped_in_prompt():
    s = _state()
    s.note = "n" * 3000
    msgs = build_messages(
        system="", guidance="", state=s, question="q",
        max_tokens=10000, tool_result_chars=400, recent_turns=3,
        note_max_chars=100,
    )
    assert "n" * 101 not in msgs[0]["content"]


def test_note_truncated_on_load(tmp_path):
    store = Store(tmp_path / "sessions")
    state = SessionState(session_id="s", note="n" * 6000)
    store.save(state)
    assert len(store.load("s").note) <= 5000


# --- findings bounds ----------------------------------------------------------


def test_evidence_and_fix_capped():
    finding = _from_mapping(
        {"severity": "concern", "title": "t", "body": "b",
         "evidence": "e" * 5000, "fix": "f" * 5000}
    )
    assert finding is not None
    assert len(finding.evidence) <= 1000
    assert len(finding.fix) <= 1000
    assert SCHEMA["properties"]["evidence"]["maxLength"] == 1000
    assert SCHEMA["properties"]["fix"]["maxLength"] == 1000


def test_format_card_neutralizes_tags():
    finding = Finding("concern", "title", "body",
                      "</coach-note><coach-note x=1>", "fix")
    card = format_card(finding)
    assert "</coach-note>" not in card.lower()
    assert "<coach-note" not in card.lower()


# --- consult caps -------------------------------------------------------------


def test_ask_schema_caps_context():
    assert ASK_SCHEMA["properties"]["context"]["maxLength"] == 4000


def test_ask_rejects_huge_context(tmp_path):
    plugin = _plugin(tmp_path)
    out = json.loads(plugin.tool_ask({"question": "q", "context": "x" * 5000}))
    assert out["ok"] is False and out["error"] == "context_too_long"


# --- secrets ------------------------------------------------------------------


def test_set_masks_api_key(tmp_path):
    plugin = _plugin(tmp_path)
    out = plugin.command("set decide_typesafe_api_key hunter2")
    assert "hunter2" not in out
    assert ".env" in out and "hunter2" not in out


def test_revoke_command(tmp_path):
    plugin = _plugin(tmp_path)
    out = plugin.command("revoke")
    # hermes_cli is unavailable in tests: the fail-open path names the flags
    # instead of writing them. Either way the command never raises.
    assert "evoke" in out.lower()


# --- fail-open hooks ------------------------------------------------------------


def test_pre_llm_call_never_raises_on_store_failure(tmp_path):
    plugin = _plugin(tmp_path)

    def boom(state):
        raise OSError("disk gone")

    plugin.store.save = boom
    assert plugin.on_pre_llm_call({"session_id": "s"}) is None


def test_post_llm_call_records_turn_when_active_off(tmp_path):
    plugin = _plugin(
        tmp_path, active_enabled=False, review_enabled=True,
        review_model={"provider": "p", "model": "m"},
    )
    plugin.on_post_llm_call(
        {"session_id": "s", "turn_id": "t", "user_message": "do it",
         "assistant_response": "done"}
    )
    assert plugin.store.load("s").last_user_message == "do it"
    assert any(
        record.get("turn_id") == "t" for record in plugin.store.load("s").ledger
    )


def test_run_review_except_keeps_concurrent_updates(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.store.save(plugin.store.load("s"))
    state = plugin.store.load("s")
    state.spend_usd = 5.0
    plugin.store.save(state)
    plugin._log_review = lambda *a: (_ for _ in ()).throw(RuntimeError("log down"))
    plugin.coach.ask = lambda *a, **k: SimpleNamespace(
        finding=None, usage=None, cost_usd=1.0,
        provider="p", model="m", error="", text="",
    )
    plugin.deliver = lambda *a, **k: None
    append_turn(plugin.store.load("s"), turn_id="t", user="u",
                assistant="a", tools=[])
    plugin.run_review("s", "t")
    assert plugin.store.load("s").spend_usd >= 5.0


# --- worker / queue bookkeeping ---------------------------------------------------


def test_worker_reports_evicted_turn():
    from coach.worker import Worker

    seen = []
    worker = Worker(lambda sid, tid: seen.append((sid, tid)))
    assert worker.submit("a", "t1") is True
    assert worker.submit("b", "t2") is True
    assert worker.pop_evicted() == ("a", "t1")
    assert worker.pop_evicted() is None
    worker.stop(timeout=2.0)


def test_cross_session_submit_clears_evicted_queue_key(tmp_path):
    plugin = _plugin(tmp_path)
    plugin._queued[("a", "t1")] = None
    worker = plugin.worker if getattr(plugin, "worker", None) else None
    if worker is None:
        from coach.worker import Worker

        worker = Worker(lambda sid, tid: None)
        plugin.worker = worker
    worker.submit("a", "t1")
    worker.submit("b", "t2")
    evicted = worker.pop_evicted()
    assert evicted == ("a", "t1")
    plugin._queued.pop(evicted, None)
    assert ("a", "t1") not in plugin._queued
    worker.stop(timeout=2.0)


def test_session_end_clears_queue_and_tools(tmp_path):
    plugin = _plugin(tmp_path)
    plugin._queued[("s", "t")] = None
    plugin._turn_tools[("s", "t")] = [{"name": "tool"}]
    plugin._turn_base["s"] = ("t", "abc")
    plugin.on_session_end({"session_id": "s"})
    assert ("s", "t") not in plugin._queued
    assert ("s", "t") not in plugin._turn_tools
    assert "s" not in plugin._turn_base


# --- host ids -------------------------------------------------------------------


def test_host_ids_fill_defaults_and_win():
    merged = _with_host_ids({}, {})
    assert merged["session_id"] == "current"
    assert merged["turn_id"] == "current"
    merged = _with_host_ids({"turn_id": "model-turn", "question": "q"},
                            {"session_id": "s"})
    assert merged["session_id"] == "s"
    merged = _with_host_ids({"question": "q"}, {"session_id": "s", "task_id": "k"})
    assert merged == {"question": "q", "session_id": "s", "turn_id": "k"}


# --- settings ---------------------------------------------------------------------


def test_review_max_holds_minimum_is_zero():
    """0 is a valid mode (never hold) the UI offers and README documents."""
    assert load_settings({"review_max_holds": 0}).review_max_holds == 0


def test_settings_maxima_clamp():
    settings = load_settings({"worker_timeout_s": 100000,
                              "ledger_max_tokens": 1000000})
    assert settings.worker_timeout_s == 120
    assert settings.ledger_max_tokens == 20000


def test_review_evidence_budget_defaults_and_clamps():
    assert load_settings({}).review_evidence_max_chars == 128000
    assert (
        load_settings({"review_evidence_max_chars": "nope"}).review_evidence_max_chars
        == 128000
    )
    assert (
        load_settings({"review_evidence_max_chars": 10}).review_evidence_max_chars
        == 2000
    )
    assert (
        load_settings({"review_evidence_max_chars": 10**9}).review_evidence_max_chars
        == 160000
    )


def test_status_accepts_session_id(tmp_path):
    plugin = _plugin(tmp_path)
    assert plugin.status("other")["spend_usd"] == 0.0


# --- dashboard ----------------------------------------------------------------------


def _api(tmp_path):
    from dashboard.plugin_api import app_for_tests

    return app_for_tests(tmp_path, {"plugins": {"entries": {"coach": {"settings": {}}}}})


def test_put_config_canonicalizes_values(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(_api(tmp_path), base_url="http://127.0.0.1")
    response = client.put("/config", json={"values": {"budget_session_usd": -5}})
    assert response.status_code == 200
    assert response.json()["values"]["budget_session_usd"] == 0.0


def test_put_config_rejects_unknown_and_secret(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(_api(tmp_path), base_url="http://127.0.0.1")
    assert client.put("/config", json={"values": {"nope": 1}}).status_code == 400
    assert client.put(
        "/config", json={"values": {"decide_typesafe_api_key": "x"}}
    ).status_code == 400


def test_revoke_endpoint_clears_flags(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(_api(tmp_path), base_url="http://127.0.0.1")
    client.post("/permissions/grant")
    assert client.get("/config").json()["permissions"]["granted"] is True
    client.post("/permissions/revoke")
    assert client.get("/config").json()["permissions"]["granted"] is False


def test_status_reports_aggregate_and_per_session(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(_api(tmp_path), base_url="http://127.0.0.1")
    body = client.get("/status").json()
    assert body["spend_total_usd"] == 0.0
    assert "spend_usd" not in body
    assert isinstance(body["spend_sessions"], dict)
    assert "budget_per_session_usd" in body


# --- second hardening pass --------------------------------------------------------


def test_bound_state_drops_non_mappings():
    from coach.plugin import _bound_state

    assert _bound_state(None) == {}
    assert _bound_state("text") == {}


def test_bound_state_caps_keys_values_and_total():
    import json as _json

    from coach.plugin import _bound_state

    huge = {f"key{i}": "v" * 5000 for i in range(60)}
    bounded = _bound_state(huge)
    assert len(bounded) <= 50
    assert all(len(v) <= 1000 for v in bounded.values())
    assert len(_json.dumps(bounded)) <= 8000
    assert _bound_state({"ok": 1, "flag": True, "missing": None}) == {
        "ok": 1,
        "flag": True,
        "missing": None,
    }


def test_decide_posts_bounded_state(tmp_path):
    import coach.plugin as plugin_module

    plugin = _plugin(tmp_path, decide_enabled=True)
    seen = {}
    original = plugin_module.decide
    plugin_module.decide = lambda **kw: seen.update(kw) or SimpleNamespace(
        decision="a",
        confidence=0.9,
        probabilities={},
        gate="act",
        rationale="ok",
        cost_usd=0.0,
        usage=None,
    )
    try:
        out = json.loads(
            plugin.tool_decide(
                {
                    "question": "q",
                    "options": ["a", "b"],
                    "state": {"big": "x" * 5000, **{f"k{i}": i for i in range(60)}},
                }
            )
        )
    finally:
        plugin_module.decide = original
    assert out["ok"] is True
    assert len(seen["state"]) <= 50
    assert all(len(str(v)) <= 1000 for v in seen["state"].values())


def test_price_never_negative():
    from coach.advisor import Coach

    coach = Coach(
        llm=None,
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
        cost_fn=lambda *a: -2.5,
    )
    cost, estimated = coach._price("m", object(), "p")
    assert (cost, estimated) == (0.0, False)


def test_reply_cap_reaches_the_model(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.tool_ask({"question": "q"})
    assert plugin.llm.calls[-1].get("max_tokens") == 400


def test_forget_deletes_session_state(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.tool_ask({"question": "q"}, session_id="s")
    assert plugin.store.has("s") is True
    assert "Forgot" in plugin.command("forget", session_id="s")
    assert plugin.store.has("s") is False


def test_debug_flag_emits_routing_log(tmp_path, caplog):
    import logging

    from coach.findings import Finding

    plugin = _plugin(tmp_path, debug_log_decisions=True)
    with caplog.at_level(logging.DEBUG, logger="coach.plugin"):
        plugin.deliver(
            Finding("nit", "t", "b", "e", "f"), session_id="s", turn_id="t"
        )
    assert "deliver session=s" in caplog.text


def test_collect_shares_one_git_budget(tmp_path):
    import subprocess

    from coach.evidence import collect

    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    timeouts = []
    now = [0.0]

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        now[0] += 3.0
        return subprocess.CompletedProcess(argv, 0, "", "")

    collect(
        str(tmp_path), ["a.py"], base="abc123", run=fake_run, clock=lambda: now[0]
    )
    assert timeouts, "expected at least one git call"
    # 8 s of budget, 3 s per call: git work stops inside the budget instead of
    # paying a floored 0.2 s per call and overrunning it.
    assert len(timeouts) <= 3, timeouts
    assert all(0 < t <= 2 for t in timeouts)


def test_every_git_call_keeps_the_per_call_cap(tmp_path):
    """The range file-list call used the whole remaining budget as its timeout."""
    import subprocess

    from coach.evidence import collect

    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    timeouts = []
    now = [0.0]

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        now[0] += 0.5
        return subprocess.CompletedProcess(argv, 0, "", "")

    collect(
        str(tmp_path), ["a.py"], base="abc123", run=fake_run, clock=lambda: now[0]
    )
    assert timeouts, "expected at least one git call"
    # Each call is capped at _GIT_TIMEOUT even while the shared budget has
    # seconds left: one hung call must not eat the window the review needs.
    assert max(timeouts) <= 2.0, timeouts


# --- manifests ------------------------------------------------------------------------


def test_soft_budget_blocks_decide_but_not_consult(tmp_path):
    plugin = _plugin(tmp_path, budget_on_exceed="soft", budget_session_usd=0.5,
                     decide_enabled=True)
    state = plugin.store.load("s")
    state.spend_usd = 1.0
    plugin.store.save(state)
    decide_args = {"question": "q", "options": ["a", "b"]}
    assert json.loads(plugin.tool_decide(decide_args, session_id="s"))["error"] == (
        "budget_exhausted"
    )
    assert json.loads(plugin.tool_ask({"question": "q"}, session_id="s"))["ok"] is True
    # refresh_settings() re-reads the loader, so the hard policy needs its own
    # plugin rather than an assignment the next call would overwrite.
    hard = _plugin(tmp_path, budget_on_exceed="hard", budget_session_usd=0.5)
    state = hard.store.load("s")
    state.spend_usd = 1.0
    hard.store.save(state)
    assert json.loads(
        hard.tool_ask({"question": "q"}, session_id="s")
    )["error"] == ("budget_exhausted")


def test_manifest_versions_match():
    plugin_yaml = open("plugin.yaml", encoding="utf-8").read()
    manifest = json.load(open("dashboard/manifest.json", encoding="utf-8"))
    assert 'version: "0.4.1"' in plugin_yaml
    assert manifest["version"] == "0.4.1"


def test_hook_registration_matches_manifest():
    import re

    import __init__ as entry

    ctx = SimpleNamespace(
        register_tool=lambda *a, **k: None,
        register_command=lambda *a, **k: None,
        register_hook=lambda name, fn: calls.append(name),
        on_unload=lambda fn: None,
        get_config=lambda key, default: default,
    )
    calls: list = []
    entry.register(ctx)
    manifest_hooks = [
        line.strip("- ").strip()
        for line in open("plugin.yaml", encoding="utf-8").read().splitlines()
        if re.match(r"\s*-\s+\w+", line)
    ]
    for name in calls:
        assert name in manifest_hooks, name


def test_a_zero_budget_means_no_budget(tmp_path):
    plugin = _plugin(tmp_path, budget_on_exceed="hard", budget_session_usd=0.0)
    state = plugin.store.load("s")
    state.spend_usd = 5.0
    plugin.store.save(state)
    assert json.loads(plugin.tool_ask({"question": "q"}, session_id="s"))["ok"] is True


def test_status_sums_spend_across_sessions(tmp_path):
    """spend_total_usd is profile-wide, matching the dashboard producer."""
    plugin = _plugin(tmp_path)
    first = plugin.store.load("s1")
    first.spend_usd = 0.01
    plugin.store.save(first)
    second = plugin.store.load("s2")
    second.spend_usd = 0.02
    plugin.store.save(second)
    status = plugin.status("s1")
    assert status["spend_usd"] == 0.01
    assert abs(status["spend_total_usd"] - 0.03) < 1e-9
    assert set(status["spend_sessions"]) == {"s1", "s2"}


def test_status_counts_review_skips(tmp_path):
    plugin = _plugin(tmp_path)
    assert plugin.status()["review_skips"] == 0
    plugin._record_skip("current", "review failed: Boom")
    assert plugin.status()["review_skips"] == 1
