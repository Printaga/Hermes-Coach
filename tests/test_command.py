# tests/test_command.py
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.support import ConsultLlm as Llm


def _p(tmp_path):
    return CoachPlugin(
        llm=Llm(),
        store=Store(tmp_path),
        settings=load_settings({"coach_model": {"provider": "openrouter", "model": "x"}}),
    )


def _p_with_own_logs(tmp_path):
    """A plugin whose log dir is under this test's tmp_path, not its parent."""
    return CoachPlugin(
        llm=Llm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings({"coach_model": {"provider": "openrouter", "model": "x"}}),
    )


def test_status_lists_modes_and_spend(tmp_path):
    out = _p(tmp_path).command("status")
    assert "Active" in out and "Review" in out and "$" in out


def test_unconfigured_status_explains(tmp_path):
    p = CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings({}))
    assert "Unconfigured" in p.command("status")


def test_set_toggles_persist_through_callback(tmp_path):
    saved = {}
    p = CoachPlugin(
        llm=Llm(),
        store=Store(tmp_path),
        settings=load_settings({"coach_model": {"provider": "openrouter", "model": "x"}}),
        set_setting=lambda k, v: saved.__setitem__(k, v),
    )
    assert "off" in p.command("active off").lower()
    assert saved["active_enabled"] is False


def test_unknown_subcommand_prints_help(tmp_path):
    assert "/coach" in _p(tmp_path).command("wat")


def test_last_prints_the_advice_the_coach_gave(tmp_path):
    p = _p(tmp_path)
    assert "No coach advice" in p.command("last", session_id="s1")
    p.tool_ask({"question": "Is the parser ok?"}, session_id="s1")
    out = p.command("last", session_id="s1")
    assert "Advice body." in out
    assert "Is the parser ok?" in out


def test_pending_prints_the_note_body(tmp_path):
    from coach.findings import Finding
    p = _p(tmp_path)
    p.deliver(Finding("concern", "Unchecked write", "Writes before validating.",
                      "a.py:1", "Validate first."),
              session_id="s1", turn_id="t1")
    out = p.command("pending", session_id="s1")
    assert "Unchecked write" in out and "Writes before validating." in out


def test_log_lists_recent_findings(tmp_path):
    from coach.findings import Finding

    p = _p(tmp_path)

    class R:
        finding = Finding("concern", "Unchecked write", "Writes first.", "a.py:1", "Validate.")
        usage = None
        cost_usd = 0.0
        provider = "p"
        model = "m"
        error = ""

    p._log_review("s1", "t1", R())
    out = p.command("log", session_id="s1")
    assert "Unchecked write" in out and "Writes first." in out


def test_review_log_is_trimmed(tmp_path):
    from coach.plugin import _trim_log

    path = tmp_path / "big.jsonl"
    path.write_text("".join("x" * 120 + "\n" for _ in range(2000)), encoding="utf-8")
    _trim_log(path, keep=10)
    assert len(path.read_text().splitlines()) == 10


def test_last_can_list_several_entries(tmp_path):
    p = _p(tmp_path)
    p.tool_ask({"question": "first question"}, session_id="s1")
    p.tool_ask({"question": "second question"}, session_id="s1")
    out = p.command("last", session_id="s1")
    assert "second question" in out and "first question" not in out
    out = p.command("last 2", session_id="s1")
    assert "second question" in out and "first question" in out


def test_last_falls_back_to_another_session(tmp_path):
    p = _p(tmp_path)
    p.tool_ask({"question": "only question"}, session_id="s1")
    out = p.command("last", session_id="unknown-session")
    assert "only question" not in out and "(from session" not in out
    assert "No coach advice" in out


def test_decisions_are_logged_and_listed(tmp_path):
    from types import SimpleNamespace

    p = _p(tmp_path)
    p._log_decision(
        "s1", "t1",
        SimpleNamespace(decision="client.py", confidence=0.9, gate="act",
                        rationale="client owns the loop", cost_usd=0.0),
        "jev-openrouter",
    )
    out = p.command("log", session_id="s1")
    assert "[decide] client.py" in out and "client owns the loop" in out
    assert "Last decision: client.py" in p.command("status", session_id="s1")


def test_log_prints_the_whole_finding_record(tmp_path):
    import json

    from coach.commands import _log_name

    p = _p_with_own_logs(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    body = "B" * 600
    evidence = "E" * 800
    fix = "F" * 700
    record = {
        "kind": "review",
        "session_id": "s1",
        "turn_id": "t1",
        "severity": "concern",
        "title": "Guard the loop",
        "body": body,
        "evidence": evidence,
        "fix": fix,
        "provider": "openrouter",
        "model": "x",
        "cost_usd": 0.001,
        "input_tokens": 10,
        "output_tokens": 5,
        "error": "",
        "at": "2026-11-07T18:00:00+00:00",
    }
    (logs / f"{_log_name('s1')}.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    out = p.command("log", session_id="s1")
    # The record carried evidence and fix all along; /coach log used to drop
    # them and cut the body at 200 chars without a marker.
    assert body in out and evidence in out and fix in out
    assert "turn t1" in out and "at 2026-11-07 18:00:00" in out
    assert "openrouter/x" in out and "$0.001000" in out


def test_log_marks_a_clipped_legacy_field_and_full_prints_it(tmp_path):
    import json

    from coach.commands import _log_name

    p = _p_with_own_logs(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    body = "L" * 5000
    (logs / f"{_log_name('s1')}.jsonl").write_text(
        json.dumps({"kind": "review", "title": "Long", "body": body}) + "\n",
        encoding="utf-8",
    )
    compact = p.command("log", session_id="s1")
    assert "chars hidden" in compact and body not in compact
    full = p.command("log 5 full", session_id="s1")
    assert body in full


def test_log_reports_unreadable_lines_instead_of_hiding_them(tmp_path):
    import json

    from coach.commands import _log_name

    p = _p_with_own_logs(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    good = json.dumps({"kind": "review", "title": "Fine", "body": "ok"})
    (logs / f"{_log_name('s1')}.jsonl").write_text(
        "{not json\n" + good + "\n", encoding="utf-8"
    )
    out = p.command("log", session_id="s1")
    assert "Fine" in out and "1 unreadable log line(s) skipped" in out


def test_decision_log_keeps_the_question_and_options(tmp_path):
    from types import SimpleNamespace

    p = _p_with_own_logs(tmp_path)
    p._log_decision(
        "s1", "t1",
        SimpleNamespace(decision="b", confidence=0.8, gate="act",
                        rationale="cheaper", cost_usd=0.0004),
        "jev-openrouter",
        question="Which store?",
        options=["a", "b"],
    )
    out = p.command("log", session_id="s1")
    assert "question: Which store?" in out
    assert "options: a; b" in out
    assert "backend jev-openrouter" in out


def test_shutdown_stops_the_worker(tmp_path):
    from coach.worker import Worker

    p = _p(tmp_path)
    p.worker = Worker(lambda session_id, turn_id: None)
    p.shutdown()
    assert p.worker.submit("s", "t") is False


def test_model_set_stores_a_plain_mapping(tmp_path):
    saved = {}
    p = CoachPlugin(
        llm=Llm(), store=Store(tmp_path),
        settings=load_settings({"coach_model": {"provider": "openrouter", "model": "x"}}),
        set_setting=lambda key, value: saved.__setitem__(key, value),
    )
    out = p.command("model consult opencode-go/glm-5.3")
    assert "consult" in out.lower()
    assert saved["consult_model"] == {"provider": "opencode-go", "model": "glm-5.3"}


def test_set_stores_the_canonical_value(tmp_path):
    from coach.plugin import CoachPlugin
    from coach.settings import load_settings
    from coach.state import Store
    from tests.support import ConsultLlm

    saved = {}
    plugin = CoachPlugin(
        llm=ConsultLlm(),
        store=Store(tmp_path),
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
        set_setting=lambda key, value: saved.setdefault(key, value) is not None,
    )
    out = plugin.command("set budget_session_usd 99999")
    assert "1000" in out
    assert saved["budget_session_usd"] == 1000.0
