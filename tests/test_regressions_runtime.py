# Cross-cutting regression tests for issues found while building the plugin.
#
# Each test pins one behavior that broke, misled, or silently degraded the
# plugin during development or live verification. Unit tests next to each
# module cover the happy paths; these tests cover the failure modes.

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from coach.delivery import format_card
from coach.evidence import collect
from coach.findings import Finding, is_deliverable, parse_finding
from coach.ledger import append_turn, build_messages, tool_card
from coach.plugin import (
    CoachPlugin,
)
from coach.settings import load_settings
from coach.state import Store
from tests.support import ConsultLlm as _ConsultLlm

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
# State, advice history, and logs
# ---------------------------------------------------------------------------


def test_advice_ring_keeps_only_the_newest_entries(tmp_path):
    plugin = _plugin(tmp_path)
    for index in range(6):
        plugin.tool_ask(
            {"question": f"question {index}"},
            session_id="s",
            turn_id=f"t{index}",
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
    from coach.logs import log_name as _log_name
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
        (tmp_path / "logs" / f"{_log_name('s1')}.jsonl").read_text(encoding="utf-8").strip()
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


def test_an_oversized_user_message_is_truncated_not_dropped(tmp_path):
    state = Store(tmp_path).load("unused")
    append_turn(
        state, turn_id="t1", user="KEEP-ME " + "U" * 10_000, assistant="a", tools=[]
    )
    messages = build_messages(
        system="",
        guidance="",
        state=state,
        question=None,
        max_tokens=100,
        tool_result_chars=10,
        recent_turns=1,
    )
    body = messages[1]["content"]
    assert body
    assert len(body) <= 400
    assert body.rstrip().endswith("U")


def test_last_resort_keeps_the_newest_user_turns(tmp_path):
    state = Store(tmp_path).load("unused")
    for index, filler in enumerate("ABCD"):
        append_turn(
            state,
            turn_id=f"t{index}",
            user=f"message-{index} " + filler * 2000,
            assistant="a",
            tools=[],
        )
    messages = build_messages(
        system="",
        guidance="",
        state=state,
        question=None,
        max_tokens=300,
        tool_result_chars=10,
        recent_turns=0,
    )
    body = messages[1]["content"]
    assert "message-0" not in body
    assert "A" * 20 not in body
    assert "D" * 20 in body


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
    assert "/coach send" in card


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
    status_argv, status_kwargs = next(
        (argv, kwargs)
        for argv, kwargs in calls
        if "status" in argv  # the first call enumerates filter drivers
    )
    diff_argv = next(
        argv for argv, _ in calls if "diff" in argv and "--stat" not in argv
    )
    assert status_argv[0] == "git"
    assert "core.quotePath=false" in status_argv
    assert "core.fsmonitor=false" in status_argv
    assert "status" in status_argv
    assert status_argv.index("status") > status_argv.index("core.fsmonitor=false")
    assert "--porcelain" in status_argv and "-z" in status_argv
    assert "--untracked-files=all" in status_argv
    assert status_kwargs["timeout"] == 2
    assert status_kwargs["shell"] is False
    assert diff_argv[diff_argv.index("diff") + 1 : diff_argv.index("HEAD")] == [
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
    ]
    assert "core.quotePath=false" in diff_argv
    assert "(new file)" in text


def test_evidence_includes_new_files_inside_new_directories(tmp_path):
    new_dir = tmp_path / "newdir"
    new_dir.mkdir()
    (new_dir / "file.py").write_text("print('hi')\n", encoding="utf-8")

    def fake_run(argv, **kwargs):
        import subprocess

        if "status" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "?? newdir/file.py\0", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    text = collect(str(tmp_path), ["newdir/file.py"], run=fake_run)
    assert "print('hi')" in text


def test_evidence_skips_paths_that_escape_on_resolve(tmp_path, monkeypatch):
    import coach.evidence as evidence_module

    def boom_resolve(self, *args, **kwargs):
        raise RuntimeError("symlink loop")

    monkeypatch.setattr(Path, "resolve", boom_resolve)
    assert evidence_module._safe_paths(tmp_path, ["a.py"]) == []


def test_evidence_heads_reject_symlink_swaps(tmp_path):
    from coach.evidence import _heads

    outside = tmp_path.parent / "outside-evidence.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "swap.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    seen = []

    def reader(path):
        seen.append(path)
        return path.read_text(encoding="utf-8")

    assert _heads(tmp_path, ["swap.py"], reader) == ""
    assert seen == []

    inside = tmp_path / "inside.py"
    inside.write_text("print('hi')\n", encoding="utf-8")
    assert "print('hi')" in _heads(tmp_path, ["inside.py"], reader)


def test_review_skips_unreadable_changes_instead_of_reviewing_air(tmp_path):
    from tests.support import PAY, review_plugin

    plugin = review_plugin(tmp_path)
    plugin.collect_evidence = lambda cwd, paths, base="", **kw: "   "
    plugin.review_now = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("empty evidence must not reach the model")
    )
    assert plugin.on_pre_verify(**PAY) is None
