# tests/test_evidence.py
import subprocess

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


def test_absolute_path_outside_the_project_is_refused(tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope")
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, "", "")
    assert collect(str(tmp_path), [str(outside)], run=fake_run) == ""


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
        if "[patch truncated]" in text:
            assert "patch below is truncated" in text, limit
