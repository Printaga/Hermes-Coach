"""The session change ledger: what this session touched, not only this turn.

The host's ``pre_verify`` payload is turn-scoped. ``changed_paths`` comes from
``agent._turn_file_mutation_paths``, which is reset at the start of every
turn, so a completion claim that covers several turns named files the reviewer
never received -- and the reviewer, correctly, reported them as unverifiable.
This module remembers the session's own paths (and the non-file actions that
leave no diff at all) together with the turn that touched them, so the gate
can hand the reviewer the earlier work labelled as such.

Pure: no host, no state, no I/O. The plugin owns when entries are recorded and
when the ledger is persisted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "MAX_ACTIONS",
    "MAX_PATHS",
    "MAX_PRIOR_PATHS",
    "PATH_CHARS",
    "SessionChanges",
    "action_line",
    "mutation_paths",
]

# The host's own set (agent/tool_result_classification.py): the file tools whose
# landed paths it reports. Mirroring it keeps the record the same shape as the
# gate's accepted input.
MUTATING_TOOLS = frozenset({"write_file", "patch"})

# Tools that change durable state without leaving a diff the reviewer can
# read. Their one-line record is corroboration that the action happened, never
# proof of what it produced; the evidence block says so.
ACTION_TOOLS = frozenset(
    {
        "memory",
        "skill_manage",
        "cronjob_manage",
        "manage_connections",
        "kanban_create",
        "kanban_complete",
        "kanban_request_review",
    }
)

MAX_PATHS = 500
MAX_ACTIONS = 24
# Bound on the earlier-turn paths handed to one evidence collection: the file
# list must stay countable and the git budget bounded.
MAX_PRIOR_PATHS = 200
PATH_CHARS = 1024
ACTION_CHARS = 160

# The V4A patch body is parsed for headers; a body larger than this is not a
# patch shape the host extracts, so it is not parsed here either.
_MAX_PATCH_BODY = 200_000


def _as_paths(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _landed(result: Any) -> list[str]:
    """The concrete paths a mutation result reports, when it reports any."""
    if not isinstance(result, str):
        return []
    try:
        data = json.loads(result.strip())
    except (TypeError, ValueError):
        return []
    if not isinstance(data, Mapping):
        return []
    files = _as_paths(data.get("files_modified"))
    if files:
        return files
    resolved = str(data.get("resolved_path") or "").strip()
    return [resolved] if resolved else []


def _failed(result: Any) -> bool:
    """True when the result itself says the mutation did not apply."""
    if not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except (TypeError, ValueError):
        return False
    if not isinstance(data, Mapping):
        return False
    return data.get("success") is False or bool(data.get("error"))


# V4A patch headers, same shapes the host's extractor accepts: an optional
# space class after ``***`` and after the keyword.
_V4A_FILE_HEADER = re.compile(r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$", re.MULTILINE)
_V4A_MOVE_HEADER = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", re.MULTILINE)


def _declared(tool_name: str, args: Mapping[str, Any]) -> list[str]:
    mode = "replace" if tool_name == "write_file" else (args.get("mode") or "replace")
    if mode == "replace":
        path = str(args.get("path") or "").strip()
        return [path] if path else []
    if mode != "patch":
        return []
    body = args.get("patch") or ""
    if not isinstance(body, str) or not body or len(body) > _MAX_PATCH_BODY:
        return []
    paths = [match.group(1).strip() for match in _V4A_FILE_HEADER.finditer(body)]
    for match in _V4A_MOVE_HEADER.finditer(body):
        paths.extend((match.group(1).strip(), match.group(2).strip()))
    return [path for path in paths if path]


def mutation_paths(tool_name: str, args: Mapping[str, Any] | None, result: Any = None) -> list[str]:
    """Paths one file-mutating tool call touched; landed paths win.

    Same rule the host applies to ``_turn_file_mutation_paths``, so a path the
    gate receives from the ledger matches one it would have received from the
    host, except that this survives the turn boundary.
    """
    if tool_name not in MUTATING_TOOLS:
        return []
    if _failed(result):
        # A refused or failed edit changed nothing; recording its declared
        # target would put a path in the evidence the reviewer counts as a
        # change that never happened.
        return []
    safe_args = args if isinstance(args, Mapping) else {}
    landed = _landed(result)
    return landed or _declared(tool_name, safe_args)


def action_line(tool_name: str, args: Mapping[str, Any] | None) -> str:
    """One bounded line naming a state-changing action that leaves no diff."""
    if tool_name not in ACTION_TOOLS:
        return ""
    try:
        rendered = json.dumps(dict(args or {}), ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        rendered = ""
    rendered = " ".join(rendered.split())[:ACTION_CHARS]
    return f"{tool_name} · {rendered}" if rendered else tool_name


class SessionChanges:
    """Insertion-ordered paths and actions for one session, bounded.

    ``turn`` is the coach's own counter, not a host id: the host sends no
    turn id to ``pre_verify``, so the plugin counts ``pre_llm_call`` turn
    transitions itself.
    """

    def __init__(
        self,
        paths: Iterable[Mapping[str, Any]] | None = None,
        actions: Iterable[Mapping[str, Any]] | None = None,
    ) -> None:
        self._paths: dict[str, int] = {}
        self._actions: list[dict[str, Any]] = []
        for item in paths or ():
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path") or "").strip()
            if path:
                self._paths[path[:PATH_CHARS]] = _turn(item.get("turn"))
        self._trim_paths()
        for item in actions or ():
            if not isinstance(item, Mapping):
                continue
            line = str(item.get("line") or "").strip()
            if line:
                self._actions.append({"line": line[:ACTION_CHARS], "turn": _turn(item.get("turn"))})
        del self._actions[:-MAX_ACTIONS]

    def add_path(self, path: str, turn: int) -> None:
        text = str(path or "").strip()
        if not text:
            return
        key = text[:PATH_CHARS]
        self._paths.pop(key, None)
        self._paths[key] = int(turn)
        self._trim_paths()

    def add_action(self, line: str, turn: int) -> None:
        text = str(line or "").strip()
        if not text:
            return
        self._actions.append({"line": text[:ACTION_CHARS], "turn": int(turn)})
        del self._actions[:-MAX_ACTIONS]

    def _trim_paths(self) -> None:
        overflow = len(self._paths) - MAX_PATHS
        if overflow <= 0:
            return
        for key in list(self._paths)[:overflow]:
            del self._paths[key]

    def items(self) -> list[tuple[str, int]]:
        return list(self._paths.items())

    def paths(self) -> list[str]:
        return list(self._paths)

    def before(self, turn: int) -> list[str]:
        """Paths last touched before ``turn`` -- the earlier turns' work."""
        return [path for path, seen in self._paths.items() if seen < int(turn)]

    def actions(self) -> list[str]:
        """Every recorded action, labelled with the turn that ran it."""
        return [
            f"turn {int(item.get('turn') or 0)} · {item.get('line') or ''}"
            for item in self._actions
        ]

    def actions_before(self, turn: int) -> list[str]:
        """Earlier-turn actions that left no diff, labelled with their turn."""
        return [
            f"turn {int(item.get('turn') or 0)} · {item.get('line') or ''}"
            for item in self._actions
            if int(item.get("turn") or 0) < int(turn)
        ]

    def to_paths(self) -> list[dict[str, Any]]:
        return [{"path": path, "turn": turn} for path, turn in self._paths.items()]

    def to_actions(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._actions]


def _turn(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
