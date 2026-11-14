# tests/test_evidence.py
import subprocess
from pathlib import Path

from coach.evidence import _filter_overrides, _other_dirty, collect


def test_untracked_files_are_included(tmp_path):
    (tmp_path / "new.py").write_text("print('hi')\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "diff" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "?? new.py\0", "")

    text = collect(str(tmp_path), ["new.py"], run=fake_run)
    assert "(new file)" in text and "print('hi')" in text


def test_non_repo_falls_back_to_file_heads(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, "", "not a git repository")

    text = collect(str(tmp_path), ["a.py"], run=fake_run)
    assert "x = 1" in text


def test_budget_is_respected(tmp_path):
    (tmp_path / "big.py").write_text("y" * 50000, encoding="utf-8")

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, "", "")

    assert len(collect(str(tmp_path), ["big.py"], max_chars=500)) <= 600


def test_parent_traversal_is_refused(tmp_path):
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, "", "")

    assert collect(str(tmp_path), ["../secret.txt"], run=fake_run) == ""


def test_absolute_path_inside_the_project_is_relativized(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, "", "not a repo")
    text = collect(str(tmp_path), [str(target)], run=fake_run)
    assert "x = 1" in text


def test_absolute_path_outside_the_project_is_read_as_content_only(tmp_path):
    """Reported by the host, so it is read -- as content, never as a diff.

    Dropping it silently was the bug: a skill file edited from a project
    session vanished and the reviewer called the claim unverifiable.
    """
    outside = tmp_path.parent / "outside-note-secret.md"
    outside.write_text("nope", encoding="utf-8")

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, "", "")

    text = collect(str(tmp_path), [str(outside)], run=fake_run)
    assert "nope" in text
    assert "outside the working directory" in text
    from coach.evidence import _safe_paths

    assert _safe_paths(tmp_path, [str(outside)]) == []


def test_absolute_and_relative_spellings_of_one_path_are_deduped(tmp_path):
    from coach.evidence import _safe_paths

    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    assert _safe_paths(tmp_path, [str(target), "a.py"]) == ["a.py"]


def test_other_dirty_strips_the_cwd_prefix(tmp_path):
    """A session started in a subdirectory counts its own work as foreign."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "tracked.txt").write_text("x\n", encoding="utf-8")

    def fake_run(argv, **kw):
        if "--show-prefix" in argv:
            return subprocess.CompletedProcess(argv, 0, "sub/\n", "")
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, " M sub/tracked.txt\0", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert _other_dirty(tmp_path / "sub", fake_run, ["tracked.txt"], lambda: 8.0) == 0


def test_a_failed_filter_probe_is_not_cached(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("", encoding="utf-8")
    calls = []

    def boom(argv, **kw):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 1)

    assert _filter_overrides(tmp_path, boom, 1.0) == []
    assert _filter_overrides(tmp_path, boom, 1.0) == []
    assert len(calls) == 2


def test_git_is_called_with_literal_pathspecs(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    collect(str(tmp_path), ["a.py"], run=fake_run)
    path_calls = [argv for argv in calls if "status" in argv or "diff" in argv]
    assert path_calls and all("--literal-pathspecs" in argv for argv in path_calls)


def test_a_truncated_patch_is_declared_in_the_coverage_block(tmp_path):
    (tmp_path / "big.py").write_text("y" * 3000 + "\n", encoding="utf-8")

    def fake_run(argv, **kw):
        if "diff" in argv and "HEAD" not in argv:
            return subprocess.CompletedProcess(argv, 0, "y" * 3000 + "\n", "")
        return subprocess.CompletedProcess(argv, 128, "", "")

    for limit in (600, 800, 1000, 1200, 1600, 2000):
        text = collect(str(tmp_path), ["big.py"], max_chars=limit, run=fake_run)
        if "[patch truncated" in text:
            assert "patch below is truncated" in text, limit


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    return tmp_path


def test_a_new_file_larger_than_four_kilobytes_is_shown_whole(tmp_path):
    """An 8 KB new source file was cut mid-function at the old 4 KB cap."""
    root = _git_repo(tmp_path)
    body = "\n".join(f"def f{index}(): return {index}" for index in range(400))
    (root / "changes.py").write_text(body + "\n", encoding="utf-8")

    text = collect(str(root), ["changes.py"], max_chars=128_000)

    assert "def f0(): return 0" in text
    assert "def f399(): return 399" in text
    assert "[file truncated" not in text


def test_an_oversized_file_reports_how_much_was_not_reviewed(tmp_path):
    from coach.evidence import _FILE_HEAD_CHARS, _heads

    (tmp_path / "huge.py").write_text("x" * 500_000, encoding="utf-8")

    text = _heads(
        tmp_path,
        ["huge.py"],
        lambda path: path.read_text(encoding="utf-8")[:_FILE_HEAD_CHARS],
        budget=160_000,
    )

    assert "first 160000 chars of ~500000 bytes shown" in text
    assert "the rest was not reviewed" in text


def test_the_path_overflow_count_ignores_duplicates(tmp_path, monkeypatch):
    from coach import evidence

    monkeypatch.setattr(evidence, "_MAX_PATHS", 2)
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")

    text = collect(str(tmp_path), ["a.py", "a.py", "b.py", "c.py"])

    assert "1 reported path(s) were beyond the 2-path limit" in text


def test_earlier_session_rows_beyond_the_cap_are_counted():
    from coach.evidence import _bounded_rows

    rows = "\n".join(f"A\tf{index}.py" for index in range(300))
    out = _bounded_rows(rows, 200)

    assert out.count("\n") == 200  # 200 rows plus the counted marker
    assert "[+100 more earlier-session files]" in out


def test_files_past_the_contents_budget_are_named_not_dropped(tmp_path):
    from coach.evidence import _HEADS_TOTAL_CHARS, _heads

    for index in range(200):
        (tmp_path / f"f{index}.py").write_text("x" * 1000, encoding="utf-8")

    text = _heads(
        tmp_path,
        [f"f{index}.py" for index in range(200)],
        lambda path: path.read_text(encoding="utf-8"),
    )

    assert "[not shown:" in text
    assert f"over the {_HEADS_TOTAL_CHARS}-byte contents budget" in text
    assert "f0.py" in text


def test_a_clipped_patch_names_the_files_it_did_not_show(tmp_path):
    root = _git_repo(tmp_path)
    (root / "first.py").write_text("a = 1\n" * 200, encoding="utf-8")
    (root / "second.py").write_text("b = 2\n" * 200, encoding="utf-8")

    text = collect(str(root), ["first.py", "second.py"], max_chars=1800)

    assert "[patch truncated — not shown:" in text
    assert "second.py" in text.split("[patch truncated")[1]


def test_a_long_name_list_still_names_a_file(tmp_path):
    """Long paths must not collapse the note back to the bare marker."""
    root = _git_repo(tmp_path)
    long_a = "a" * 90 + ".py"
    long_b = "b" * 90 + ".py"
    (root / long_a).write_text("x = 1\n" * 300, encoding="utf-8")
    (root / long_b).write_text("y = 2\n" * 300, encoding="utf-8")

    text = collect(str(root), [long_a, long_b], max_chars=1400)

    assert "— not shown:" in text
    assert long_a in text or long_b in text


def test_an_exhausted_git_budget_is_declared(tmp_path, monkeypatch):
    """A section that is empty because git timed out must not read as empty."""
    from coach import evidence

    monkeypatch.setattr(evidence, "_GIT_TOTAL_BUDGET", 0.0)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    text = collect(str(tmp_path), ["a.py"])

    assert "git time budget ran out" in text


def test_paths_beyond_the_limit_are_declared(tmp_path, monkeypatch):
    from coach import evidence

    monkeypatch.setattr(evidence, "_MAX_PATHS", 3)
    for index in range(5):
        (tmp_path / f"f{index}.py").write_text("x = 1\n", encoding="utf-8")

    text = collect(str(tmp_path), [f"f{index}.py" for index in range(5)])

    assert "beyond the 3-path limit" in text


def test_outside_paths_report_their_overflow(tmp_path, monkeypatch):
    import uuid

    from coach import evidence
    from coach.evidence import _outside_paths

    monkeypatch.setattr(evidence, "_MAX_OUTSIDE", 1)
    made = []
    for index in range(3):
        target = tmp_path.parent / f"coach-outside-{uuid.uuid4().hex}-{index}.md"
        target.write_text("x", encoding="utf-8")
        made.append(str(target))
    try:
        found, dropped = _outside_paths(tmp_path, made)
    finally:
        for name in made:
            Path(name).unlink(missing_ok=True)

    assert len(found) == 1
    assert dropped == 2
