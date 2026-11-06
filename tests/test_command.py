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
    assert "only question" in out and "(from session" in out


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
