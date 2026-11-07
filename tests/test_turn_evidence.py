"""Turn-scoped evidence: the completion gate must see work already committed.

Agents commit before claiming completion, so a worktree-only diff is empty at
exactly the moment the gate runs. These tests pin the range-based collector and
the turn-base snapshot that feeds it.
"""

import os
import shutil
import subprocess

import pytest

from coach.evidence import collect, head
from coach.plugin import _REVIEW_SYSTEM, _SYSTEM
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

    # Priority when the budget is tiny: the file list and the coverage line
    # survive; the stat is the first thing dropped.
    text = collect(str(root), ["f.py"], base=base, max_chars=200)
    assert len(text) <= 200
    assert "coverage" in text or "files changed" in text

    roomy = collect(str(root), ["f.py"], base=base, max_chars=900)
    assert "--- files changed (1) ---" in roomy
    assert "--- stat" in roomy
    assert "truncated" in roomy


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
    assert text.endswith("[patch truncated]")


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


def test_review_prompt_gives_missing_evidence_its_own_severity():
    assert "unverifiable" in _REVIEW_SYSTEM
    assert "coverage limit" in _REVIEW_SYSTEM
    assert "never" in _REVIEW_SYSTEM



def _run(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True)


def _commit_add(root):
    _run(root, "add", "-A")

def test_the_file_list_survives_a_clipped_patch(tmp_path):
    root = _repo(tmp_path)
    (root / "README").write_text("hi\n")
    _commit(root, "initial")
    (root / "small.py").write_text("x = 1\n")
    (root / "huge.py").write_text("y = 1\n" * 4000)
    _run(root, "add", "-A")

    text = collect(str(root), ["small.py", "huge.py"], max_chars=900)

    assert "--- files changed (2) ---" in text
    assert "small.py" in text
    assert "huge.py" in text
    assert text.endswith("[patch truncated]")
    assert "has NOT been shown to be unchanged" in text


def test_the_header_leads_with_stat_then_a_counted_file_list(tmp_path):
    root = _repo(tmp_path)
    (root / "README").write_text("hi\n")
    _commit(root, "initial")
    (root / "a.py").write_text("x = 1\n")
    (root / "b.py").write_text("y = 2\n")
    _run(root, "add", "-A")

    text = collect(str(root), ["a.py", "b.py"])

    assert text.index("--- stat") < text.index("--- files changed")
    assert text.index("--- files changed") < text.index("--- coverage ---")
    assert "2 changed file(s) listed; 2 path(s) supplied by the host" in text
    assert "has NOT been shown to be unchanged" not in text


def test_a_repo_fsmonitor_never_runs_during_collect(tmp_path):
    root = _repo(tmp_path)
    (root / "f.py").write_text("x = 1\n")
    marker = root / "fsmonitor-ran"
    script = root / "hook.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    _run(root, "config", "core.fsmonitor", str(script))
    _run(root, "add", "f.py")
    if marker.exists():
        marker.unlink()

    collect(str(root), ["f.py"])

    assert not marker.exists()


def test_evidence_reads_refuse_non_regular_files(tmp_path):
    from coach.evidence import _read_text

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert _read_text(fifo) is None


def test_evidence_reads_are_bounded(tmp_path):
    from coach.evidence import _read_text

    big = tmp_path / "big.txt"
    big.write_text("x" * 100_000)
    assert len(_read_text(big, 50)) == 50


def test_the_turn_range_is_described_in_full_not_by_reported_paths(tmp_path):
    """A 29-file commit filtered to two paths looked like a two-file commit."""
    root = _repo(tmp_path)
    (root / "README").write_text("hi\n")
    base = _commit(root, "initial")
    (root / "only.py").write_text("x = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test_x(): assert True\n")
    (root / "docs.md").write_text("# docs\n")
    _commit(root, "turn work")

    text = collect(str(root), ["only.py"], base=base)

    assert "--- files changed (3) ---" in text
    assert "tests/test_x.py" in text
    assert "docs.md" in text
    assert "the patch shows only the paths the host reported" in text
    # The patch itself stays scoped to what the host reported.
    assert "only.py" in text
    assert "+def test_x" not in text


def test_repo_filter_drivers_are_disabled_for_every_git_call(tmp_path):
    root = _repo(tmp_path)
    (root / "README").write_text("hi\n")
    _commit(root, "initial")
    (root / ".gitattributes").write_text("*.dat filter=evil\n")
    marker = root / "filter-ran"
    script = root / "hook.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    script.chmod(0o755)
    _run(root, "config", "filter.evil.clean", str(script))
    (root / "x.dat").write_text("payload\n")
    if marker.exists():
        marker.unlink()

    text = collect(str(root), ["x.dat"])

    assert "x.dat" in text
    assert not marker.exists(), "the repo's clean filter executed during collect"


def test_the_filter_override_reaches_the_git_argv(tmp_path):
    import subprocess

    root = _repo(tmp_path)
    (root / "README").write_text("hi\n")
    _run(root, "config", "filter.evil.clean", "cat")
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        if "config" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "filter.evil.clean cat\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    collect(str(root), ["README"], run=fake_run)
    assert any("filter.evil.clean=" in " ".join(argv) for argv in seen)


def test_new_files_are_seen_from_a_subdirectory(tmp_path):
    root = _repo(tmp_path)
    (root / "README").write_text("hi\n")
    _commit(root, "initial")
    sub = root / "pkg"
    sub.mkdir()
    (sub / "new.py").write_text("x = 1\n")

    text = collect(str(sub), ["new.py"])

    assert "new.py" in text
    assert "--- new files ---" in text


def test_the_file_list_announces_its_cap():
    from coach.evidence import _file_list

    rows = "\n".join(f"A\tf{i}.py" for i in range(205))
    out = _file_list(
        lambda *a, **k: None,
        __import__("pathlib").Path("."),
        ["f0.py"],
        remaining=lambda: 0.0,
        range_names=rows,
    )
    assert "[+5 more files]" in out
