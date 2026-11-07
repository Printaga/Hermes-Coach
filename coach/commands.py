"""The /coach command surface.

Every handler here runs against the plugin through ``self``; nothing in this
module imports the plugin, so the dependency runs one way. The class body of
``CoachPlugin`` used to hold these 19 methods plus their formatting helpers,
which is what made a 2 100-line file out of four separate responsibilities.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings import (
    DEFAULTS,
    ModelChoice,
    Settings,
    load_settings,
    split_provider_model,
)
from .state import safe_session_name as _state_safe_name

log = logging.getLogger(__name__)

_MODEL_KEYS = {"coach_model", "active_model", "review_model", "consult_model"}
_MODE_KEYS = {
    "coach": "coach_model",
    "active": "active_model",
    "review": "review_model",
    "consult": "consult_model",
}


_HELP = (
    "Coach commands:\n"
    "  /coach status                    configuration, modes, spend\n"
    "  /coach last                      the last advice the coach gave\n"
    "  /coach log [n] [full]            last reviews/decisions with their context\n"
    "  /coach active on|off             per-turn Active review\n"
    "  /coach review on|off             pre-completion Review gate\n"
    "  /coach decide on|off             coach_decide tool\n"
    "  /coach model [mode] [provider/model]   set or show a model; mode is\n"
    "                                  coach, active, review or consult\n"
    "  /coach set <key> <value>         any config key and value\n"
    "  /coach guidance                  guidance paths and contents\n"
    "  /coach pending                   undelivered ambient notes\n"
    "  /coach send                      inject pending notes now\n"
    "  /coach pause                     toggle background reviews and holds\n"
    "  /coach grant                     allow per-mode model overrides\n"
    "  /coach revoke                    remove per-mode model overrides\n"
    "  /coach forget                    delete this session\u2019s coach state"
)


def _model_label(choice: ModelChoice | None) -> str:
    return f"{choice.provider}/{choice.model}" if choice is not None else "none set"


def _on_off(value: Any) -> str:
    return "on" if value else "off"


def _bool_arg(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"on", "true", "yes", "1", "enable", "enabled"}:
        return True
    if text in {"off", "false", "no", "0", "disable", "disabled"}:
        return False
    return None


def _raw_settings(settings: Settings) -> dict[str, Any]:
    """Rebuild the raw config mapping so one change can be re-validated."""
    raw: dict[str, Any] = {}
    for key in DEFAULTS:
        value = getattr(settings, key, DEFAULTS[key])
        if isinstance(value, ModelChoice):
            raw[key] = {"provider": value.provider, "model": value.model}
        else:
            raw[key] = value
    return raw


def _parse_model(text: str) -> tuple[bool, ModelChoice | None]:
    value = str(text or "").strip()
    if value.lower() in {"none", "clear", "inherit", "off"}:
        return True, None
    provider, model, sep = split_provider_model(value)
    if not sep or not provider or not model:
        return False, None
    return True, ModelChoice(provider=provider, model=model)


def _coerce_setting(key: str, text: str) -> tuple[bool, Any]:
    """Coerce one command value to the type its setting expects."""
    default = DEFAULTS.get(key)
    if key in _MODEL_KEYS:
        return _parse_model(text)
    if isinstance(default, bool):
        value = _bool_arg(text)
        return value is not None, value
    if isinstance(default, int):
        try:
            return True, int(str(text).strip())
        except ValueError:
            return False, None
    if isinstance(default, float):
        try:
            value = float(str(text).strip())
        except ValueError:
            return False, None
        # "inf" would parse here and then be discarded by load_settings, so the
        # command would report success for a value the plugin never uses.
        if not math.isfinite(value):
            return False, None
        return True, value
    return True, str(text)


def _as_turn(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _log_name(session_id: str) -> str:
    # Same encoding as session files: sanitising unsafe characters to "_" made
    # "task/1" and "task_1" share one log, which leaked reviews between chats.
    raw = str(session_id or "").strip()
    return _state_safe_name(raw) if raw else "session"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# The writer stores body/evidence/fix at their schema maxima (600/1000/1000),
# so the default bound only bites on hand-edited or legacy records. `full`
# raises it for those instead of silently dropping the tail.
_LOG_FIELD_LIMIT = 4000
_LOG_FIELD_LIMIT_FULL = 20000


def _log_value(value: Any) -> str:
    """Render one logged field, flattening option lists."""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _log_detail(value: Any, limit: int) -> tuple[str, bool]:
    """Return the field text and whether the tail had to be clipped.

    The marker is part of the text on purpose: a silent slice made a shortened
    finding look like the model's complete answer.
    """
    text = _log_value(value)
    if len(text) <= limit:
        return text, False
    hidden = len(text) - limit
    return (
        f"{text[:limit]}… (+{hidden} chars hidden; add 'full' to /coach log)",
        True,
    )


def _log_time(value: Any) -> str:
    """A compact, human-readable timestamp for one log line."""
    text = str(value or "").strip()
    return text.replace("T", " ")[:19] if "T" in text else text[:19]


def _log_meta(row: Mapping[str, Any]) -> str:
    """The pricing/attribution line every event can carry."""
    parts: list[str] = []
    provider = str(row.get("provider") or "").strip()
    model = str(row.get("model") or "").strip()
    if provider and model:
        parts.append(f"{provider}/{model}")
    elif model or provider:
        parts.append(model or provider)
    backend = str(row.get("backend") or "").strip()
    if backend:
        parts.append(f"backend {backend}")
    cost = row.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        parts.append(f"${float(cost):.6f}")
    tokens_in = row.get("input_tokens")
    tokens_out = row.get("output_tokens")
    if isinstance(tokens_in, int) and isinstance(tokens_out, int):
        parts.append(f"tokens {tokens_in}→{tokens_out}")
    return " · ".join(parts)


class CommandsMixin:
    """The /coach subcommands, mixed into CoachPlugin."""

    def command(self, raw: str, **kw: Any) -> str:
        """Run one /coach subcommand; unknown input prints the help."""
        self.refresh_settings()
        session_id = str(kw.get("session_id") or "current")
        text = str(raw or "").strip()
        if text.startswith("/coach"):
            text = text[len("/coach") :].strip()
        parts = text.split()
        if not parts:
            return _HELP.rstrip()
        name, args = parts[0].lower(), parts[1:]
        try:
            if name == "status":
                return self._status_text(session_id)
            if name == "last":
                return self._last_advice_text(session_id, args)
            if name == "log":
                return self._log_text(session_id, args)
            if name in {"active", "review", "decide"}:
                return self._toggle(f"{name}_enabled", name, args)
            if name == "model":
                return self._model_command(args)
            if name == "set":
                return self._set_command(args)
            if name == "guidance":
                return self._guidance_text()
            if name == "pending":
                return self._pending_text(session_id)
            if name == "send":
                return self._send_pending(session_id)
            if name == "pause":
                return self._toggle_pause()
            if name == "grant":
                return self._grant()
            if name == "revoke":
                return self._revoke()
            if name == "forget":
                return self._forget(session_id)
        except Exception as exc:  # a command must not break the host
            log.exception("coach command %s failed", name)
            return f"Coach: {name} failed ({type(exc).__name__})."
        return _HELP.rstrip()

    def _apply(self, key: str, value: Any) -> bool:
        """Persist one setting and re-validate the in-memory copy."""
        if key not in DEFAULTS:
            return False
        # Serialize the snapshot→validate→write swap: two /coach set calls on
        # different keys would otherwise both write from the same stale copy.
        with self._store_lock:
            stored = value.as_dict() if isinstance(value, ModelChoice) else value
            raw = _raw_settings(self.settings)
            raw[key] = stored
            # Store what the loader would use, not what was typed: the dashboard
            # already does, and two writers must not disagree about a value.
            canonical = load_settings(raw)
            # Persist only a value the loader actually produced. Falling back to
            # the raw typed value would bypass validation and save it anyway.
            if not hasattr(canonical, key):
                return False
            saved = getattr(canonical, key)
            if isinstance(saved, ModelChoice):
                saved = saved.as_dict()
            if self.set_setting is not None:
                result = self.set_setting(key, saved)
                if result is False:
                    return False
            self.settings = canonical
            self.coach.settings = self.settings
            return True

    def _toggle(self, key: str, label: str, args: list[str]) -> str:
        value = _bool_arg(args[0]) if args else None
        if value is None:
            return f"Usage: /coach {label} on|off"
        if not self._apply(key, value):
            return f"Coach: could not save {key}."
        return f"{label.capitalize()} {_on_off(value)}."

    def _resolved(self, mode: str) -> ModelChoice | None:
        if mode == "coach":
            return self.settings.coach_model
        return self.settings.model_for(mode)

    def _model_command(self, args: list[str]) -> str:
        if not args:
            lines = [f"Coach model: {_model_label(self.settings.coach_model)}"]
            for mode in ("active", "review", "consult"):
                lines.append(
                    f"{mode.capitalize()} model: {_model_label(self._resolved(mode))}"
                )
            return "\n".join(lines)
        mode = args[0].lower()
        key = _MODE_KEYS.get(mode)
        if key is None:
            return (
                "Usage: /coach model [coach|active|review|consult] "
                "[provider/model|clear]"
            )
        if len(args) < 2:
            return f"{mode.capitalize()} model: {_model_label(self._resolved(mode))}"
        valid, choice = _parse_model(" ".join(args[1:]))
        if not valid:
            return "Use provider/model, or clear to inherit the coach model."
        if not self._apply(key, choice):
            return f"Coach: could not save {key}."
        return f"{mode.capitalize()} model set to {_model_label(choice)}."

    def _set_command(self, args: list[str]) -> str:
        if len(args) < 2:
            return "Usage: /coach set <key> <value>"
        key = args[0]
        if key not in DEFAULTS:
            return f"Unknown setting: {key}. Run /coach status for current values."
        if key.endswith("_api_key"):
            return (
                f"{key} belongs in .env, not config.yaml: set it there and "
                "restart Hermes. Secrets are never written to the host config."
            )
        valid, value = _coerce_setting(key, " ".join(args[1:]))
        if not valid:
            return f"Invalid value for {key}."
        if not self._apply(key, value):
            return f"Coach: could not save {key}."
        current = getattr(self.settings, key, None)
        if isinstance(current, ModelChoice):
            return f"{key} = {_model_label(current)}"
        return f"{key} = {current!r}"

    def _status_text(self, session_id: str = "current") -> str:
        settings = self.settings
        state = self.store.load(session_id)
        lines: list[str] = []
        if settings.is_configured():
            lines.append("Coach configured.")
        else:
            lines.append(
                "Unconfigured: no coach model set. "
                "Run /coach model coach <provider/model>."
            )
        lines.append(f"Coach model: {_model_label(settings.coach_model)}")
        for mode in ("active", "review", "consult"):
            lines.append(
                f"{mode.capitalize()} model: {_model_label(self._resolved(mode))}"
            )
        lines.append(
            f"Active: {_on_off(settings.active_enabled)} "
            f"({settings.active_cadence})"
        )
        lines.append(
            f"Review: {_on_off(settings.review_enabled)} "
            f"({settings.review_authority})"
        )
        lines.append(
            f"Decide: {_on_off(settings.decide_enabled)} "
            f"({settings.decide_backend})"
        )
        lines.append(
            f"Spend: ${state.spend_usd:.4f} of "
            f"${settings.budget_session_usd:.2f} "
            f"({settings.budget_on_exceed})"
        )
        lines.append(f"Pending: {len(state.pending)} note(s)")
        last_event = self._last_event_text(session_id)
        if last_event:
            lines.append(last_event)
        if self._paused:
            lines.append("Paused: yes")
        if state.last_error:
            lines.append(f"Last error: {state.last_error}")
        return "\n".join(lines)

    def _last_event_text(self, session_id: str = "current") -> str:
        """One line summarising the newest logged event, for status."""
        path = self.store.root.parent / "logs" / f"{_log_name(session_id)}.jsonl"
        try:
            line = path.read_text(encoding="utf-8", errors="replace").splitlines()[-1]
            row = json.loads(line)
            if not isinstance(row, Mapping):
                return ""
        except (OSError, IndexError, ValueError):
            return ""
        if str(row.get("kind") or "") == "decide":
            return (
                f"Last decision: {row.get('decision')} "
                f"({row.get('gate')}, {row.get('confidence')})"
            )
        return (
            f"Last review: [{row.get('severity') or 'none'}] "
            f"{row.get('title') or 'no finding'}"
        )

    def _guidance_text(self) -> str:
        cwd = str(self.cwd() or "")
        lines: list[str] = []
        for label, scope, path in (
            ("Global", "global", self.guidance.root / "guidance.md"),
            ("Project", "project", Path(cwd) / "COACH.md"),
        ):
            body = self.guidance.read(scope, cwd).strip()
            lines.append(f"{label} guidance ({path}):")
            lines.append(body or "(empty)")
        return "\n".join(lines)

    def _state_with(self, session_id: str) -> Any:
        """Load the session state; never leak another session's content.

        A read-only command stays in its own session: other chats' advice
        must not surface in the current context.
        """
        return self.store.load(session_id)

    def _last_advice_text(
        self, session_id: str = "current", args: list[str] | None = None
    ) -> str:
        try:
            count = max(1, min(5, int((args or ["1"])[0])))
        except (TypeError, ValueError):
            count = 1
        state = self._state_with(session_id)
        rows = [row for row in state.advice if isinstance(row, Mapping)]
        if not rows and isinstance(state.last_advice, Mapping) and state.last_advice:
            rows = [state.last_advice]
        if not rows:
            decision = state.last_decision if isinstance(state.last_decision, Mapping) else {}
            if decision.get("decision"):
                return self._format_decision(decision)
            return "No coach advice recorded in this session yet."
        if count == 1:
            row = rows[-1]
            lines = []
            question = str(row.get("question") or "").strip()
            if question:
                lines.append(f"Question: {question}")
            lines.append(str(row.get("text") or ""))
            meta = " / ".join(
                part
                for part in (str(row.get("intent") or ""), str(row.get("model") or ""))
                if part
            )
            if meta:
                lines.append(f"({meta})")
            return "\n".join(lines)
        out = [f"Last {min(count, len(rows))} advice entries (newest first):"]
        for index, row in enumerate(reversed(rows[-count:]), 1):
            question = str(row.get("question") or "").strip()[:120]
            text = str(row.get("text") or "").strip()[:160]
            intent = str(row.get("intent") or "")
            out.append(f"{index}. [{intent}] {question}")
            if text:
                out.append(f"   {text}")
        return "\n".join(out)

    @staticmethod
    def _format_decision(decision: Mapping[str, Any]) -> str:
        return "\n".join(
            [
                f"Last decision: {decision.get('decision')} "
                f"({decision.get('gate')}, confidence {decision.get('confidence')})",
                f"Question: {decision.get('question') or '(not recorded)'}",
                str(decision.get("rationale") or ""),
            ]
        )

    def _log_text(self, session_id: str = "current", args: list[str] | None = None) -> str:
        """Print recent logged events with the context the record carries."""
        raw_args = [str(arg).strip().lower() for arg in (args or [])]
        full = "full" in raw_args
        numbers = [arg for arg in raw_args if arg.isdigit()]
        try:
            count = int(numbers[0]) if numbers else 5
        except (TypeError, ValueError):
            count = 5
        count = max(1, min(20, count))
        path = self.store.root.parent / "logs" / f"{_log_name(session_id)}.jsonl"
        if not path.exists():
            return "No coach reviews logged for this session yet."
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "No coach reviews logged for this session yet."
        rows: list[Mapping[str, Any]] = []
        skipped = 0
        for line in lines[-count:]:
            try:
                row = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(row, Mapping):
                # A stray scalar line parses without raising and would blow
                # up row.get() below; skip it like an unparseable line.
                skipped += 1
                continue
            rows.append(row)
        if not rows:
            message = "No coach reviews logged for this session yet."
            if skipped:
                message += f" ({skipped} unreadable log line(s) skipped.)"
            return message
        limit = _LOG_FIELD_LIMIT_FULL if full else _LOG_FIELD_LIMIT
        clipped = False
        out = [f"Last {len(rows)} coach event(s):"]
        for row in rows:
            kind = str(row.get("kind") or "review")
            where = []
            turn = str(row.get("turn_id") or "").strip()
            if turn:
                where.append(f"turn {turn}")
            when = _log_time(row.get("at"))
            if when:
                where.append(f"at {when}")
            suffix = f" ({', '.join(where)})" if where else ""
            if kind == "decide":
                out.append(
                    f"- [decide] {row.get('decision') or '(none)'} "
                    f"({row.get('gate') or 'unknown'} gate, "
                    f"confidence {row.get('confidence')}){suffix}"
                )
                fields = (
                    ("question", row.get("question")),
                    ("options", row.get("options")),
                    ("rationale", row.get("rationale")),
                )
            else:
                out.append(
                    f"- [{row.get('severity') or 'none'}] "
                    f"{row.get('title') or 'no finding'}{suffix}"
                )
                # The record has always carried these; dropping them left the
                # reader with a title and half a body.
                fields = (
                    ("body", row.get("body")),
                    ("evidence", row.get("evidence")),
                    ("fix", row.get("fix")),
                )
            for label, value in fields:
                text, was_clipped = _log_detail(value, limit)
                clipped = clipped or was_clipped
                if text:
                    out.append(f"  {label}: {text.replace(chr(10), chr(10) + '  ')}")
            meta = _log_meta(row)
            if meta:
                out.append(f"  meta: {meta}")
            error = str(row.get("error") or "").strip()
            if error:
                text, was_clipped = _log_detail(error, limit)
                clipped = clipped or was_clipped
                out.append(f"  error: {text}")
        if skipped:
            out.append(f"({skipped} unreadable log line(s) skipped.)")
        if clipped and not full:
            out.append("Some fields were clipped; /coach log <n> full prints them whole.")
        return "\n".join(out)

    def _pending_text(self, session_id: str = "current") -> str:
        state = self.store.load(session_id)
        notes = [note for note in state.pending if isinstance(note, Mapping)]
        if not notes:
            return "No pending coach notes."
        lines = [f"{len(notes)} pending coach note(s):"]
        for note in notes:
            # Show what _send_pending will inject: text, or body when an older
            # writer left the text empty.
            body = str(note.get("text") or note.get("body") or "").strip()
            snippet = f" — {body[:160]}" if body else ""
            lines.append(
                f"- [{note.get('severity') or 'note'}] "
                f"{note.get('title') or 'note'}"
                f"{snippet} "
                f"(expires turn {_as_turn(note.get('expires_at_turn'))})"
            )
        return "\n".join(lines)

    def _send_pending(self, session_id: str = "current") -> str:
        if self.inject is None:
            return "No injection channel available; notes stay pending."
        with self._store_lock:
            state = self.store.load(session_id)
            deliverable = [
                note
                for note in state.pending
                if isinstance(note, Mapping)
                and str(note.get("text") or note.get("body") or "").strip()
            ]
            if not deliverable:
                return "No pending coach notes."
            # Claim the notes before injecting: the host injection call runs
            # outside the lock, so a second /coach send (or the background
            # dispatcher) must not find the same entry still pending. Anything
            # that fails is re-queued below; nothing is dropped twice.
            state.pending = [
                note
                for note in state.pending
                if not (
                    isinstance(note, Mapping)
                    and str(note.get("text") or note.get("body") or "").strip()
                )
            ]
            state.updated_at = self.clock()
            self.store.save(state)
        sent = 0
        failed: list[Any] = []
        for note in deliverable:
            text = str(note.get("text") or note.get("body") or "").strip()
            if self._inject(text):
                sent += 1
            else:
                failed.append(note)
        with self._store_lock:
            state = self.store.load(session_id)
            if failed:
                # A retry keeps its place ahead of notes queued meanwhile.
                state.pending = [*failed, *state.pending]
            state.updated_at = self.clock()
            self.store.save(state)
        if not sent:
            return "Could not deliver pending notes; they stay pending."
        return f"Delivered {sent} pending coach note(s)."

    def _toggle_pause(self) -> str:
        self._paused = not self._paused
        if self._paused:
            return (
                "Coach paused: background reviews and holds are off until "
                "/coach pause again."
            )
        return "Coach resumed."

    def _grant(self) -> str:
        """Write the host's model-override trust flags, best effort."""
        from .trustflags import write

        return write(True)

    def _revoke(self) -> str:
        """Clear the host's model-override trust flags, best effort."""
        from .trustflags import write

        return write(False)

    def _forget(self, session_id: str) -> str:
        """Delete this session's state file and review log; never raise."""
        try:
            self.store.forget(session_id)
        except Exception:
            return "Coach: could not forget this session."
        log_path = self.store.root.parent / "logs" / f"{_log_name(session_id)}.jsonl"
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass
        return "Forgot this session's coach state."
