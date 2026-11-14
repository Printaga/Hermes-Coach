# tests/test_changes.py
"""The session change ledger's pure rules."""

import json

from coach.changes import (
    MAX_ACTIONS,
    MAX_PATHS,
    SessionChanges,
    action_line,
    mutation_paths,
)


def test_replace_mode_uses_the_path_argument():
    assert mutation_paths("write_file", {"path": "a.py"}) == ["a.py"]
    assert mutation_paths("patch", {"path": "a.py", "mode": "replace"}) == ["a.py"]


def test_landed_paths_win_over_declared_targets():
    result = '{"success": true, "resolved_path": "/tmp/real.py"}'
    assert mutation_paths("write_file", {"path": "declared.py"}, result) == ["/tmp/real.py"]
    result = '{"files_modified": ["one.py", "two.py"]}'
    assert mutation_paths("patch", {"path": "x.py"}, result) == ["one.py", "two.py"]


def test_v4a_headers_are_parsed_like_the_host():
    body = (
        "*** Begin Patch\n"
        "***Update File: a.py\n"
        "*** Add File: b.py\n"
        "*** Delete File: c.py\n"
        "*** Move File: old.py -> new.py\n"
        "*** End Patch\n"
    )
    assert mutation_paths("patch", {"mode": "patch", "patch": body}) == [
        "a.py",
        "b.py",
        "c.py",
        "old.py",
        "new.py",
    ]


def test_non_mutating_tools_and_empty_args_are_ignored():
    assert mutation_paths("read_file", {"path": "a.py"}) == []
    assert mutation_paths("write_file", {}) == []
    # write_file is always replace mode (the host forces it); an unknown mode
    # on patch is not a shape the host extracts, so neither is it.
    assert mutation_paths("patch", {"mode": "other", "path": "a.py"}) == []


def test_a_failed_result_reports_no_paths():
    """A refused edit changed nothing; its declared target is not evidence."""
    result = json.dumps({"success": False, "error": "approval denied"})
    assert mutation_paths("write_file", {"path": "a.py"}, result) == []
    assert mutation_paths("patch", {"path": "a.py", "mode": "replace"}, result) == []


def test_action_lines_cover_state_changing_tools_only():
    assert action_line("read_file", {"path": "a.py"}) == ""
    line = action_line("memory", {"operations": [{"action": "add", "content": "x"}]})
    assert line.startswith("memory · ")
    assert len(line) <= len("memory · ") + 160


def test_paths_keep_the_latest_turn_and_are_bounded():
    changes = SessionChanges()
    changes.add_path("a.py", 1)
    changes.add_path("a.py", 3)
    assert changes.items() == [("a.py", 3)]
    assert changes.before(3) == []
    assert changes.before(4) == ["a.py"]

    for index in range(MAX_PATHS + 20):
        changes.add_path(f"f{index}.py", 4)
    assert len(changes.paths()) == MAX_PATHS
    assert "a.py" not in changes.paths()


def test_actions_are_bounded_and_labelled_with_their_turn():
    changes = SessionChanges()
    for index in range(MAX_ACTIONS + 5):
        changes.add_action(f"memory · fact {index}", 2)
    assert len(changes.to_actions()) == MAX_ACTIONS
    assert changes.actions_before(2) == []
    earlier = changes.actions_before(3)
    assert earlier and earlier[0].startswith("turn 2 · memory · ")


def test_serialisation_round_trips():
    changes = SessionChanges()
    changes.add_path("/tmp/a.py", 2)
    changes.add_action("memory · x", 2)
    again = SessionChanges(changes.to_paths(), changes.to_actions())
    assert again.items() == changes.items()
    assert again.to_actions() == changes.to_actions()
