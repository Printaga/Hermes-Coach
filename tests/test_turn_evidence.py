"""Turn-scoped evidence: the completion gate must see work already committed.

Agents commit before claiming completion, so a worktree-only diff is empty at
exactly the moment the gate runs. These tests pin the range-based collector and
the turn-base snapshot that feeds it.
"""

import shutil
import subprocess

import pytest

from coach.evidence import collect, head
from coach.plugin import _REVIEW_SYSTEM, _SYSTEM, CoachPlugin
from coach.state import Store
from tests.support import plugin as make_plugin

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    return tmp_path


def _commit(tmp_path, message):
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=tmp_path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@needs_git
def test_committed_work_is_reviewable(tmp_path):
    root = _repo(tmp_path)
    (root / "f.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(root, "base")
    (root / "f.py").write_text("x = 2\n", encoding="utf-8")
    _commit(root, "change")

    # The worktree is clean now: the worktree-only collector sees nothing.
    assert collect(str(root), ["f.py"]) == ""

    text = collect(str(root), ["f.py"], base=base)
    assert "committed during this turn" in text
    assert "-x = 1" in text and "+x = 2" in text
    assert "--- stat (commits since turn start" in text


@needs_git
def test_uncommitted_work_still_reported_alongside_commits(tmp_path):
    root = _repo(tmp_path)
    (root / "f.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(root, "base")
    (root / "f.py").write_text("x = 2\n", encoding="utf-8")
    _commit(root, "change")
    (root / "f.py").write_text("x = 3\n", encoding="utf-8")

    text = collect(str(root), ["f.py"], base=base)
    assert "committed during this turn" in text
    assert "uncommitted" in text
    assert "+x = 3" in text


@needs_git
def test_nothing_changed_since_the_base_yields_no_evidence(tmp_path):
    root = _repo(tmp_path)
    (root / "f.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(root, "base")
    assert collect(str(root), ["f.py"], base=base) == ""


@needs_git
def test_unusable_base_falls_back_to_the_worktree(tmp_path):
    root = _repo(tmp_path)
    (root / "f.py").write_text("x = 1\n", encoding="utf-8")
    _commit(root, "base")
    (root / "f.py").write_text("x = 2\n", encoding="utf-8")

    text = collect(str(root), ["f.py"], base="0" * 40)
    assert "not an ancestor" in text
    assert "+x = 2" in text


@needs_git
def test_oversized_diff_keeps_the_stat_summary(tmp_path):
    root = _repo(tmp_path)
    (root / "f.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(root, "base")
    (root / "f.py").write_text("y = 1\n" * 4000, encoding="utf-8")
    _commit(root, "big")

    text = collect(str(root), ["f.py"], base=base, max_chars=200)
    assert len(text) <= 200
    assert "--- stat" in text
    assert "truncated" in text


@needs_git
def test_a_partially_truncated_diff_keeps_hunks_after_the_stat(tmp_path):
    root = _repo(tmp_path)
    (root / "f.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(root, "base")
    (root / "f.py").write_text("y = 1\n" * 4000, encoding="utf-8")
    _commit(root, "big")

    text = collect(str(root), ["f.py"], base=base, max_chars=900)
    assert len(text) <= 900
    assert "--- stat" in text
    assert "committed during this turn" in text
    assert "diff --git" in text
    assert text.endswith("[diff truncated]")


@needs_git
def test_head_reports_the_current_commit_and_empty_outside_a_repo(tmp_path):
    root = _repo(tmp_path)
    (root / "f.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(root, "base")
    assert head(str(root)) == base
    assert head(str(tmp_path / "not-a-repo")) == ""


def test_turn_base_is_snapshotted_once_per_turn(tmp_path):
    p = make_plugin(tmp_path)
    commits = iter(["aaa", "bbb", "ccc"])
    p.head_commit = lambda: next(commits)

    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})  # mid-turn model call
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t2"})

    assert p._turn_base["s"] == ("t2", "bbb")


def test_turn_base_snapshot_survives_a_missing_head(tmp_path):
    p = make_plugin(tmp_path)
    p.head_commit = lambda: ""
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})
    assert "s" not in p._turn_base


def test_gate_receives_the_turn_base(tmp_path):
    p = make_plugin(tmp_path, review_enabled=True)
    seen = {}

    def evidence(cwd, paths, base=""):
        seen["base"] = base
        return "diff --git a/f.py"

    p.collect_evidence = evidence
    p.review_now = lambda **kw: None
    p._turn_base["s"] = ("t1", "deadbeef")

    p.on_pre_verify(
        session_id="s",
        turn_id="t1",
        final_response="done",
        changed_paths=["f.py"],
        cwd=str(tmp_path),
    )
    assert seen["base"] == "deadbeef"


def test_gate_still_works_without_a_recorded_base(tmp_path):
    p = make_plugin(tmp_path, review_enabled=True)
    seen = {}
    p.collect_evidence = lambda cwd, paths, base="": (
        seen.setdefault("base", base) or "diff --git a/f.py"
    )
    p.review_now = lambda **kw: None

    p.on_pre_verify(
        session_id="fresh",
        turn_id="t1",
        final_response="done",
        changed_paths=["f.py"],
        cwd=str(tmp_path),
    )
    assert seen["base"] == ""


def test_session_end_drops_the_turn_base(tmp_path):
    p = make_plugin(tmp_path)
    p._turn_base["s"] = ("t1", "abc")
    p.on_session_end({"session_id": "s"})
    assert "s" not in p._turn_base


@needs_git
def test_gate_sees_work_committed_during_the_turn(tmp_path):
    """The bug this feature fixes: commit, then claim completion."""
    root = _repo(tmp_path)
    (root / "store.py").write_text("def copy(src, dst):\n    return 1\n", encoding="utf-8")
    _commit(root, "base")

    p = make_plugin(root, review_enabled=True)
    p.cwd = lambda: str(root)
    p.on_pre_llm_call({"session_id": "s", "turn_id": "t1"})

    # The agent edits and commits, so the worktree is clean at completion.
    (root / "store.py").write_text(
        "def copy(src, dst):\n    remove(dst)\n    return 2\n", encoding="utf-8"
    )
    _commit(root, "change")
    assert collect(str(root), ["store.py"]) == ""

    captured = {}

    def fake_review(**kwargs):
        captured.update(kwargs)
        return None

    p.review_now = fake_review
    p.on_pre_verify(
        session_id="s",
        turn_id="t1",
        final_response="done, copy fixed",
        changed_paths=["store.py"],
        cwd=str(root),
    )

    evidence = captured["evidence"]
    assert "committed during this turn" in evidence
    assert "+    remove(dst)" in evidence
    assert captured["claim"] == "done, copy fixed"


def test_active_prompt_does_not_ask_for_diffs():
    assert "never raise a finding that evidence or a diff is missing" in _SYSTEM
    assert "turn summary" in _SYSTEM


def test_review_prompt_caps_missing_evidence_at_a_nit():
    assert "at most as a nit" in _REVIEW_SYSTEM
    assert "coverage limit of the plugin" in _REVIEW_SYSTEM
