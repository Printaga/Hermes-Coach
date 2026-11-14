# tests/test_release_sync.py
"""The release sync: version + changelog in, tag + GitHub Release out."""

import json
import subprocess

import pytest

from scripts.release_sync import release_notes, sync, version_of

HEAD = "a" * 40
TAG_SHA = "b" * 40


class Recorder:
    """A fake runner that answers gh/git calls and records every argv."""

    def __init__(
        self,
        *,
        tag_sha=None,
        release_body=None,
        release_url="https://x/rel",
        tag_error="HTTP 404: Not Found",
        release_error="release not found",
    ):
        self.calls = []
        self.tag_sha = tag_sha
        self.release = release_body
        self.release_url = release_url
        self.tag_error = tag_error
        self.release_error = release_error
        self.notes_seen = None

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        argv = list(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return self._ok(HEAD + "\n")
        if argv[:2] == ["gh", "api"] and "/commits/" in argv[2]:
            if self.tag_sha is None:
                return self._fail(self.tag_error)
            return self._ok(self.tag_sha + "\n")
        if argv[:3] == ["gh", "release", "view"]:
            if self.release is None:
                return self._fail(self.release_error)
            return self._ok(json.dumps({"body": self.release, "url": self.release_url}))
        if argv[:3] == ["gh", "release", "create"]:
            return self._write_notes(argv, "https://x/new")
        if argv[:3] == ["gh", "release", "edit"]:
            return self._write_notes(argv, "")
        if argv[:3] == ["gh", "api", "-X"]:
            return self._ok("{}")
        return self._ok("")

    def _write_notes(self, argv, url):
        path = argv[argv.index("--notes-file") + 1]
        with open(path, encoding="utf-8") as handle:
            self.notes_seen = handle.read().rstrip("\n")
        self.release = self.notes_seen
        return self._ok(url)

    @staticmethod
    def _ok(stdout=""):
        return subprocess.CompletedProcess(["fake"], 0, stdout, "")

    @staticmethod
    def _fail(stderr=""):
        return subprocess.CompletedProcess(["fake"], 1, "", stderr)

    def mutating(self):
        return [
            call
            for call in self.calls
            if call[:3] in (["gh", "release", "create"], ["gh", "release", "edit"])
            or (call[:3] == ["gh", "api", "-X"])
        ]


@pytest.fixture()
def changelog(tmp_path):
    path = tmp_path / "changelog.md"
    path.write_text(
        "# Changelog\n\n"
        "## [1.0.1] - 2026-11-14\n\n### Fixed\n\n- A thing.\n\n"
        "## [1.0.0] - 2026-11-10\n\n- Initial release.\n",
        encoding="utf-8",
    )
    return path


def test_version_reads_quoted_and_bare_values(tmp_path):
    quoted = tmp_path / "a.yaml"
    quoted.write_text('name: coach\nversion: "1.0.1"\n', encoding="utf-8")
    bare = tmp_path / "b.yaml"
    bare.write_text("name: coach\nversion: 2.0.0\n", encoding="utf-8")
    assert version_of(quoted) == "1.0.1"
    assert version_of(bare) == "2.0.0"


def test_version_missing_raises(tmp_path):
    path = tmp_path / "plugin.yaml"
    path.write_text("name: coach\n", encoding="utf-8")
    with pytest.raises(ValueError):
        version_of(path)


def test_release_notes_stops_at_the_next_version(changelog):
    notes = release_notes(changelog.read_text(encoding="utf-8"), "1.0.1")
    assert notes.startswith("### Fixed")
    assert "Initial release" not in notes
    assert notes.endswith("A thing.")


def test_release_notes_missing_or_empty_raises(changelog):
    text = changelog.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        release_notes(text, "9.9.9")
    empty = "## [2.0.0] - 2026-01-01\n\n## [1.0.0] - 2026-01-01\n\n- old\n"
    with pytest.raises(ValueError):
        release_notes(empty, "2.0.0")


def test_a_missing_tag_and_release_are_both_created(changelog):
    runner = Recorder()
    summary = sync(version="1.0.1", repo="o/r", changelog=changelog, run=runner, apply=True)
    assert any(action.startswith("create tag v1.0.1 at") for action in summary["actions"])
    assert "create release" in summary["actions"]
    refs = [call for call in runner.calls if call[:3] == ["gh", "api", "-X"]]
    assert refs and "ref=refs/tags/v1.0.1" in refs[0] and f"sha={HEAD}" in refs[0]
    assert (runner.notes_seen or "").startswith("### Fixed")
    assert summary["release"] == "https://x/new"


def test_an_existing_tag_is_never_moved(changelog):
    runner = Recorder(tag_sha=TAG_SHA)
    summary = sync(version="1.0.1", repo="o/r", changelog=changelog, run=runner, apply=True)
    assert summary["tag_sha"] == TAG_SHA
    assert not [call for call in runner.calls if call[:3] == ["gh", "api", "-X"]]
    assert "create release" in summary["actions"]


def test_a_matching_release_is_left_alone(changelog):
    body = release_notes(changelog.read_text(encoding="utf-8"), "1.0.1")
    runner = Recorder(tag_sha=TAG_SHA, release_body=body)
    summary = sync(version="1.0.1", repo="o/r", changelog=changelog, run=runner, apply=True)
    assert summary["actions"] == []
    assert runner.mutating() == []


def test_drifted_notes_are_rewritten(changelog):
    runner = Recorder(tag_sha=TAG_SHA, release_body="old notes")
    summary = sync(version="1.0.1", repo="o/r", changelog=changelog, run=runner, apply=True)
    assert summary["actions"] == ["update release notes"]
    edits = [call for call in runner.calls if call[:3] == ["gh", "release", "edit"]]
    assert len(edits) == 1
    assert (runner.notes_seen or "").startswith("### Fixed")


def test_check_mode_writes_nothing(changelog):
    runner = Recorder()
    summary = sync(version="1.0.1", repo="o/r", changelog=changelog, run=runner, apply=False)
    assert "create release" in summary["actions"]
    assert runner.mutating() == []


def test_a_failed_tag_read_aborts_instead_of_creating(changelog):
    """A 401/timeout is not "no such tag": abort, never POST a duplicate ref."""
    runner = Recorder(tag_error="gh: authentication failed (HTTP 401)")
    with pytest.raises(RuntimeError):
        sync(version="1.0.1", repo="o/r", changelog=changelog, run=runner, apply=True)
    assert runner.mutating() == []


def test_a_failed_release_read_aborts(changelog):
    runner = Recorder(tag_sha=TAG_SHA, release_error="dial tcp: lookup api.github.com")
    with pytest.raises(RuntimeError):
        sync(version="1.0.1", repo="o/r", changelog=changelog, run=runner, apply=True)
    assert runner.mutating() == []
