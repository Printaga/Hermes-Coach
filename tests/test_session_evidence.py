"""Prior-turn work must reach the completion reviewer.

The host's ``pre_verify.changed_paths`` is turn-scoped: the agent resets
``_turn_file_mutation_paths`` at every turn start, so a completion claim that
covers work done in earlier turns of the same session is reviewed against the
last turn's files alone and the reviewer reports the rest as "unverifiable".
The coach keeps its own session ledger so the gate can supply that work --
paths, commits, and the file contents behind them.

These tests call ``on_pre_verify`` WITHOUT ``turn_id`` on purpose: the host's
``pre_verify`` payload carries session_id, platform, model, coding, attempt,
final_response and changed_paths, and nothing else.
"""

import json
import shutil
import subprocess

import pytest

from coach.evidence import collect
from tests.support import plugin as make_plugin

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    return tmp_path


def _commit(tmp_path, message):
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=tmp_path, check=True)


def _edit(p, session_id, path):
    """One landed file edit as the host reports it to ``post_tool_call``."""
    p.on_post_tool_call(
        {
            "session_id": session_id,
            "tool_name": "patch",
            "args": {"path": str(path), "mode": "replace"},
            "result": json.dumps({"success": True}),
        }
    )


def test_a_file_changed_in_an_earlier_turn_reaches_the_reviewer(tmp_path):
    p = make_plugin(tmp_path, review_enabled=True)
    p.cwd = lambda: str(tmp_path)
    skill = tmp_path / "skill.md"
    notes = tmp_path / "notes.md"
    skill.write_text("nine: bare winetricks warning\n", encoding="utf-8")
    notes.write_text("tahoma\n", encoding="utf-8")

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    _edit(p, "s", skill)
    p.on_post_llm_call({"session_id": "s", "turn_id": "t1"})

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t2"})
    _edit(p, "s", notes)

    captured = {}
    p.review_now = lambda **kw: captured.update(kw) or None
    p.on_pre_verify(
        {
            "session_id": "s",
            "final_response": "updated skill.md and notes.md",
            "changed_paths": [str(notes)],
        }
    )

    assert captured, "the gate did not review the completion claim"
    evidence = captured["evidence"]
    assert "skill.md" in evidence
    assert "bare winetricks warning" in evidence


@needs_git
def test_committed_work_from_an_earlier_turn_is_in_the_evidence(tmp_path):
    root = _repo(tmp_path)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit(root, "base")

    p = make_plugin(root, review_enabled=True)
    p.cwd = lambda: str(root)

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    _commit(root, "turn one")
    _edit(p, "s", root / "a.py")
    p.on_post_llm_call({"session_id": "s", "turn_id": "t1"})

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t2"})
    (root / "b.py").write_text("y = 1\n", encoding="utf-8")

    captured = {}
    p.review_now = lambda **kw: captured.update(kw) or None
    p.on_pre_verify(
        {
            "session_id": "s",
            "final_response": "fixed a.py and added b.py",
            "changed_paths": ["b.py"],
        }
    )

    evidence = captured["evidence"]
    assert "committed earlier in this session" in evidence
    assert "+x = 2" in evidence


def test_the_turn_base_applies_when_the_host_sends_no_turn_id(tmp_path):
    p = make_plugin(tmp_path, review_enabled=True)
    seen = {}

    def evidence(cwd, paths, base="", **kw):
        seen.setdefault("base", base)
        return "diff --git a/f.py"

    p.collect_evidence = evidence
    p.review_now = lambda **kw: None
    p._turn_base["s"] = ("t1", "deadbeef")

    p.on_pre_verify(
        {
            "session_id": "s",
            "final_response": "done",
            "changed_paths": ["f.py"],
            "cwd": str(tmp_path),
        }
    )

    assert seen["base"] == "deadbeef"


def test_a_reported_path_outside_the_working_directory_is_not_dropped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    note = tmp_path / "home" / ".hermes" / "skills" / "x" / "SKILL.md"
    note.parent.mkdir(parents=True)
    note.write_text("9. bare winetricks warning\n", encoding="utf-8")

    text = collect(str(project), [str(note)])

    assert "SKILL.md" in text
    assert "bare winetricks warning" in text
    assert "outside the working directory" in text


def test_a_reported_relative_parent_path_is_read_from_the_working_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    sibling = tmp_path / "shared.md"
    sibling.write_text("outside note\n", encoding="utf-8")

    text = collect(str(project), ["../shared.md"])

    assert "shared.md" in text
    assert "outside note" in text


@needs_git
def test_earlier_session_rows_never_crowd_out_this_turns_file(tmp_path):
    """A long session's history must not push the turn's own file off the list.

    Measured before the fix: 200 earlier-session rows filled the list and the
    turn's file was dropped, which is the same "claim names a file that is not
    in the change set" failure the session ledger exists to remove.
    """
    root = _repo(tmp_path)
    (root / "seed.txt").write_text("s\n", encoding="utf-8")
    _commit(root, "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()

    for index in range(250):
        (root / f"early{index}.py").write_text("x = 1\n", encoding="utf-8")
    _commit(root, "earlier turn in this session")
    (root / "turn_file.py").write_text("y = 1\n", encoding="utf-8")

    prior = [f"early{index}.py" for index in range(80)]
    text = collect(
        str(root),
        ["turn_file.py", *prior],
        prior=prior,
        session_base=base,
    )

    listing = text.split("--- coverage ---")[0]
    assert "turn_file.py" in listing
    assert "more earlier-session files]" in listing
    assert len([line for line in listing.splitlines() if "\t" not in line]) < 290


@needs_git
def test_this_turns_patch_survives_a_large_earlier_session(tmp_path):
    """The block is clipped from the tail: the turn's own patch comes first."""
    root = _repo(tmp_path)
    (root / "seed.txt").write_text("s\n", encoding="utf-8")
    _commit(root, "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()

    for index in range(40):
        (root / f"early{index}.py").write_text("\n".join(f"line {n}" for n in range(60)))
    _commit(root, "earlier turn in this session")
    turn_base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    (root / "seed.txt").write_text("s\nthis turn's change\n", encoding="utf-8")

    prior = [f"early{index}.py" for index in range(40)]
    text = collect(
        str(root),
        ["seed.txt", *prior],
        base=turn_base,
        prior=prior,
        session_base=base,
        max_chars=2500,
    )

    assert "this turn's change" in text
    assert "[patch truncated" in text
    assert text.index("--- uncommitted ---") < text.index("--- committed earlier in this session ---")


def test_the_ledger_records_edits_even_when_active_review_is_off(tmp_path):
    p = make_plugin(tmp_path, review_enabled=True, active_enabled=False)
    p.cwd = lambda: str(tmp_path)
    skill = tmp_path / "skill.md"
    skill.write_text("nine\n", encoding="utf-8")
    notes = tmp_path / "notes.md"
    notes.write_text("n\n", encoding="utf-8")

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    _edit(p, "s", skill)
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t2"})
    _edit(p, "s", notes)

    captured = {}
    p.review_now = lambda **kw: captured.update(kw) or None
    p.on_pre_verify(
        {
            "session_id": "s",
            "final_response": "both files",
            "changed_paths": [str(notes)],
        }
    )

    assert "skill.md" in captured["evidence"]


def test_an_earlier_turn_memory_fact_reaches_the_reviewer(tmp_path):
    """A memory write leaves no diff; the ledger still names the action."""
    p = make_plugin(tmp_path, review_enabled=True)
    p.cwd = lambda: str(tmp_path)
    note = tmp_path / "notes.md"
    note.write_text("x\n", encoding="utf-8")

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    p.on_post_tool_call(
        {
            "session_id": "s",
            "tool_name": "memory",
            "args": {"operations": [{"action": "add", "content": "fact #189: wine hint"}]},
            "result": "saved",
        }
    )
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t2"})
    _edit(p, "s", note)

    captured = {}
    p.review_now = lambda **kw: captured.update(kw) or None
    p.on_pre_verify(
        {
            "session_id": "s",
            "final_response": "recorded the fact and updated notes",
            "changed_paths": [str(note)],
        }
    )

    evidence = captured["evidence"]
    assert "recorded session actions" in evidence
    assert "fact #189" in evidence


def test_a_failed_edit_is_not_recorded_as_a_change(tmp_path):
    """An errored tool call must not put a file in the evidence."""
    p = make_plugin(tmp_path, review_enabled=True)
    p.cwd = lambda: str(tmp_path)
    ghost = tmp_path / "ghost.md"
    note = tmp_path / "note.md"
    note.write_text("n\n", encoding="utf-8")

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    p.on_post_tool_call(
        {
            "session_id": "s",
            "tool_name": "write_file",
            "args": {"path": str(ghost)},
            "result": json.dumps({"success": False, "error": "denied"}),
            "status": "error",
        }
    )
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t2"})
    p.on_post_tool_call(
        {
            "session_id": "s",
            "tool_name": "write_file",
            "args": {"path": str(note)},
            "result": json.dumps({"success": True}),
        }
    )

    captured = {}
    p.review_now = lambda **kw: captured.update(kw) or None
    p.on_pre_verify(
        {"session_id": "s", "final_response": "wrote note.md", "changed_paths": [str(note)]}
    )

    evidence = captured["evidence"]
    assert "ghost.md" not in evidence
    assert "note.md" in evidence


def test_a_reloaded_plugin_resumes_the_session_ledger(tmp_path):
    """A plugin reload mid-session must not lose the earlier turns."""
    p = make_plugin(tmp_path, review_enabled=True)
    p.cwd = lambda: str(tmp_path)
    skill = tmp_path / "skill.md"
    skill.write_text("nine\n", encoding="utf-8")
    notes = tmp_path / "notes.md"
    notes.write_text("n\n", encoding="utf-8")

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    _edit(p, "s", skill)
    p.on_post_llm_call({"session_id": "s", "turn_id": "t1"})
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t2"})
    _edit(p, "s", notes)
    p.on_post_llm_call({"session_id": "s", "turn_id": "t2"})

    fresh = make_plugin(tmp_path, review_enabled=True)
    fresh.cwd = lambda: str(tmp_path)
    fresh.on_pre_llm_call({"session_id": "s", "turn_id": "t3"})
    captured = {}
    fresh.review_now = lambda **kw: captured.update(kw) or None
    fresh.on_pre_verify(
        {
            "session_id": "s",
            "final_response": "all of it",
            "changed_paths": [str(skill)],
        }
    )

    evidence = captured["evidence"]
    assert "notes.md" in evidence
    assert "earlier turn" in evidence


def test_a_record_before_seeding_keeps_the_persisted_history(tmp_path):
    """A reload that lands mid-turn records a path before any pre_llm_call."""
    p = make_plugin(tmp_path, review_enabled=True)
    p.cwd = lambda: str(tmp_path)
    skill = tmp_path / "skill.md"
    skill.write_text("nine\n", encoding="utf-8")
    notes = tmp_path / "notes.md"
    notes.write_text("n\n", encoding="utf-8")

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    _edit(p, "s", skill)
    p.on_post_llm_call({"session_id": "s", "turn_id": "t1"})

    fresh = make_plugin(tmp_path, review_enabled=True)
    fresh.cwd = lambda: str(tmp_path)
    _edit(fresh, "s", notes)  # tool call first: no pre_llm_call yet
    fresh.on_pre_llm_call({"session_id": "s", "turn_id": "t2"})

    captured = {}
    fresh.review_now = lambda **kw: captured.update(kw) or None
    fresh.on_pre_verify(
        {
            "session_id": "s",
            "final_response": "both",
            "changed_paths": [str(notes)],
        }
    )

    assert "skill.md" in captured["evidence"]


def test_the_session_ledger_is_bounded(tmp_path):
    from coach.changes import MAX_PATHS

    p = make_plugin(tmp_path, review_enabled=True)
    p.cwd = lambda: str(tmp_path)
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    for index in range(MAX_PATHS + 60):
        _edit(p, "s", tmp_path / f"f{index}.md")

    paths = p.session_changes("s")
    assert len(paths) <= MAX_PATHS


def test_session_end_forgets_the_ledger(tmp_path):
    p = make_plugin(tmp_path, review_enabled=True)
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    _edit(p, "s", tmp_path / "f.md")
    assert p.session_changes("s")

    p.on_session_end({"session_id": "s"})
    assert p.session_changes("s") == []
