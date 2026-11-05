# tests/test_evidence.py
import subprocess

from coach.evidence import collect


def test_untracked_files_are_included(tmp_path):
    (tmp_path / "new.py").write_text("print('hi')\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "?? new.py\n", "")

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
