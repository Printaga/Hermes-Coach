"""The coach tools, their guards, delivery, and plugin construction.

Every path through :meth:`CoachPlugin.tool_ask` returns a JSON string. Bad
input, an unconfigured coach, a child session, an exhausted budget or a
provider failure are all reported as ``{"ok": false, "error": ...}`` rather
than raised into the host. Findings reach the user through a card, a pending
ambient note, or a corrective message, according to the delivery policy.
The Review gate runs one bounded diff review before a completion claim and
fails open on every error.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .advisor import Coach
from .cards import advice_card as _advice_card
from .cards import decision_card as _decision_card
from .cards import hold_message as _hold_message
from .coerce import as_finite_float
from .commands import CommandsMixin, _as_turn
from .counters import TurnCounters
from .delivery import format_ambient, format_card, format_corrective, route
from .evidence import collect, head
from .findings import Finding
from .guidance import Guidance
from .jev import decide
from .ledger import (
    _CARDS_PER_TURN_MAX,
    _MAX_TURNS,
    append_turn,
    bound_turn,
    build_messages,
    tool_card,
)
from .lockfile import StoreLock
from .logs import append as append_log
from .logs import now_iso as _now_iso
from .prompts import PRE_SYSTEM, REVIEW_SYSTEM, SYSTEM
from .scrub import strip_delimited
from .secrets import get_secret as _get_secret
from .settings import (
    DEFAULTS,
    ModelChoice,
    Settings,
    load_settings,
    split_provider_model,
)
from .state import Store
from .worker import Worker

log = logging.getLogger(__name__)

# The host kills a hook at 30 s and then skips that callback for a while, so a
# slow review does not just lose its verdict: it silences the gate. Keep the
# whole gate (evidence + model call + store writes) inside this budget.
_GATE_BUDGET_S = 24.0
_REVIEW_TIMEOUT_S = 16.0
_GATE_MIN_REVIEW_S = 5.0
# A decide option is a label and a rationale is a paragraph: both reach the
# wire, the state file and the log, so each needs its own bound.
_OPTION_MAX = 1000
_RATIONALE_MAX = 1000
# The one display cap this module still owns; the card caps live in cards.py.
_TOOL_ARG_CHARS = 120

__all__ = [
    "ASK_DESCRIPTION",
    "ASK_SCHEMA",
    "DECIDE_DESCRIPTION",
    "DECIDE_SCHEMA",
    "CoachPlugin",
    "build_plugin",
]

ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "maxLength": 4000,
            "description": (
                "The question for the coach. It sees the bounded session ledger "
                "and answers once, without tools."
            ),
        },
        "intent": {
            "type": "string",
            "enum": ["advice", "plan", "review"],
            "description": "What kind of answer is wanted: advice, a plan, or a review.",
        },
        "context": {
            "type": "string",
            "maxLength": 4000,
            "description": "Optional extra context for this one consultation.",
        },
        "turn_id": {
            "type": "string",
            "description": "Host turn id used to enforce the per-turn call cap.",
        },
        "session_id": {
            "type": "string",
            "description": "Host session id; child sessions are refused unless allowed.",
        },
    },
    "required": ["question"],
    "additionalProperties": False,
}

_QUESTION_MAX = 4000
_CONTEXT_MAX = 4000
_OPTIONS_MIN = 2
_OPTIONS_MAX = 6
_INTENTS = {"advice", "plan", "review"}
_CHILD_SURFACES = {"subagent", "delegate"}
_CHILD_KEYS = (
    "session_id",
    "child_session_id",
    "subagent_session_id",
    "child_id",
)
_SYSTEM = SYSTEM
_PRE_SYSTEM = PRE_SYSTEM
_REVIEW_SYSTEM = REVIEW_SYSTEM
ASK_DESCRIPTION = (
    "Escalate to a stronger reviewer when it would change your next step: "
    "stuck, before substantive work, or before declaring done. Use for "
    "uncertainty, stalled/contradictory evidence, material tradeoffs, "
    "security/privacy/secrets, data loss/migration/API, arch/infra, "
    "deployment risks, or repeated debugging failures. Ask with task, "
    "findings, attempts, and the exact decision. Independent review, not a "
    "substitute for evidence/tests; follow unless they justify otherwise, "
    "saying why. Skip routine work. Returns JSON advice or an error. Relay "
    "the advice's substance in your next visible reply, attributed to the "
    "coach, so the user does not have to expand the tool result to read it."
)

DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "maxLength": 4000,
            "description": (
                "The fork to resolve. Put the deciding facts in `state`, not "
                "in the question alone."
            ),
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": _OPTIONS_MIN,
            "maxItems": _OPTIONS_MAX,
            "description": "The choices, verbatim. The model picks exactly one.",
        },
        "state": {
            "type": "object",
            "description": "Optional facts the decision should weigh.",
        },
        "turn_id": {
            "type": "string",
            "description": "Host turn id used to enforce the per-turn call cap.",
        },
        "session_id": {
            "type": "string",
            "description": "Host session id; child sessions are refused unless allowed.",
        },
    },
    "required": ["question", "options"],
    "additionalProperties": False,
}

DECIDE_DESCRIPTION = (
    "Resolve one fork with a typed decision. Returns JSON with the chosen "
    "option, confidence, a gate of act, escalate or error, and a rationale."
)


def _model_dict(choice: ModelChoice | None) -> dict[str, str] | None:
    """The wire shape the host, page and manifest share."""
    return choice.as_dict() if choice is not None else None














def _error(code: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": code}
    payload.update(extra)
    return payload


def _error_text(code: str) -> str:
    if code == "unconfigured":
        return "no coach model configured"
    if code == "same_model":
        return "coach model matches the primary model"
    return code


def _payload(payload: Mapping[str, Any] | None, kw: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(payload) if isinstance(payload, Mapping) else {}
    data.update(kw)
    return data


def _child_id(data: Mapping[str, Any]) -> str:
    for key in _CHILD_KEYS:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _tool_arg(args: Mapping[str, Any]) -> str:
    for key in ("path", "file", "file_path", "command", "query", "pattern", "url"):
        text = str(args.get(key) or "").strip()
        if text:
            return text[:_TOOL_ARG_CHARS]
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return value.strip()[:_TOOL_ARG_CHARS]
    return ""


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


_STATE_KEYS_MAX = 50
_STATE_VALUE_MAX = 1000
_STATE_JSON_MAX = 8000


def _bound_state(value: Any) -> dict[str, Any]:
    """Bound model-supplied decide state before it leaves for a third party.

    ``coach_decide.state`` is posted to the Jev backend verbatim; without a
    cap a compromised or careless model turns one decide call into an
    exfiltration and billing event. Keep flat scalar-ish values only.
    """
    if not isinstance(value, Mapping):
        return {}
    bounded: dict[str, Any] = {}
    for key, item in itertools.islice(value.items(), _STATE_KEYS_MAX):
        name = str(key)[:120]
        if isinstance(item, bool) or item is None or isinstance(item, (str, int)):
            bounded[name] = str(item)[:_STATE_VALUE_MAX] if isinstance(item, str) else item
        elif isinstance(item, float):
            # json.dumps(inf) emits `Infinity`, which is not JSON; a strict
            # receiver rejects the whole payload. The state is model-supplied,
            # so drop the value rather than ship an invalid document.
            bounded[name] = item if math.isfinite(item) else None
        else:
            try:
                text = json.dumps(item, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                continue
            bounded[name] = text[:_STATE_VALUE_MAX]
    try:
        # Measure the escaped form: jev._post serialises with the default
        # ensure_ascii=True, so a code-point count understates the wire size by
        # up to six times for non-ASCII text.
        while (
            len(json.dumps(bounded, ensure_ascii=True, default=str)) > _STATE_JSON_MAX
            and bounded
        ):
            bounded.pop(next(iter(bounded)))
    except (TypeError, ValueError):
        return {}
    return bounded


def _turn_digest(turn: Mapping[str, Any]) -> str:
    # The digest covers content only, never the turn id: the same work seen
    # twice must not earn two reviews, and a changed turn id alone must not
    # defeat the duplicate-review guard.
    parts = [
        str(turn.get("user") or ""),
        str(turn.get("assistant") or ""),
    ]
    tools = turn.get("tools")
    if isinstance(tools, list):
        for card in tools:
            if isinstance(card, Mapping):
                parts.append(
                    f"{card.get('name')}|{card.get('arg')}|{card.get('status')}"
                )
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _turns_since_review(state: Any) -> int:
    count = 0
    for record in reversed(state.ledger):
        if not isinstance(record, Mapping):
            continue
        if record.get("reviewed"):
            return count
        count += 1
    return count


def _last_reviewed_digest(state: Any) -> str:
    for record in reversed(state.ledger):
        if (
            isinstance(record, Mapping)
            and record.get("reviewed")
            and record.get("digest")
        ):
            return str(record["digest"])
    return ""


def _pre_digest(user_text: str) -> str:
    # Namespaced apart from _turn_digest: a pre-turn question and a finished
    # turn must never compare equal in the duplicate-review guard.
    return hashlib.sha1(f"pre\x1f{user_text}".encode()).hexdigest()


def _turns_since_pre_guidance(state: Any) -> int:
    count = 0
    for record in reversed(state.ledger):
        if not isinstance(record, Mapping):
            continue
        if record.get("pre_guidance"):
            return count
        count += 1
    return count


def _last_pre_digest(state: Any) -> str:
    for record in reversed(state.ledger):
        if (
            isinstance(record, Mapping)
            and record.get("pre_guidance")
            and record.get("pre_digest")
        ):
            return str(record["pre_digest"])
    return ""


def _record_turn(state: Any, turn: dict[str, Any]) -> None:
    last = state.ledger[-1] if state.ledger else None
    bounded = bound_turn(turn)
    if isinstance(last, dict) and str(last.get("turn_id")) == turn["turn_id"]:
        earlier = last.get("tools")
        incoming = bounded.get("tools")
        if isinstance(earlier, list) and isinstance(incoming, list):
            # A turn spans several model calls; keep every batch's cards, not
            # just the last one, and keep the newest when over the cap.
            merged = [*earlier, *incoming][-_CARDS_PER_TURN_MAX:]
            bounded["tools"] = merged
        elif earlier and not incoming:
            bounded.pop("tools", None)
        last.update(bounded)
    else:
        state.ledger.append(bounded)
    if len(state.ledger) > _MAX_TURNS:
        del state.ledger[: len(state.ledger) - _MAX_TURNS]
        _prune_calls(state)


def _prune_calls(state: Any) -> None:
    """Drop per-turn counters for turns no longer on the ledger.

    ``state.calls`` grows one entry per turn (call caps, tool counts, holds);
    without eviction a long session bloats the file on every save.
    """
    turn_ids = {
        str(record.get("turn_id"))
        for record in state.ledger
        if isinstance(record, dict)
    }
    TurnCounters(state.calls).prune(turn_ids)


def _charge(state: Any, cost: Any) -> None:
    """Add one reply's cost, clamped finite and non-negative, onto a state."""
    charged = as_finite_float(cost, 0.0)
    if charged < 0:
        # A negative price would lower the running spend and reopen the budget
        # stop; it is a pricing error, not a refund.
        log.warning("coach: negative cost ignored: %.9f", charged)
        charged = 0.0
    state.spend_usd = round(as_finite_float(state.spend_usd, 0.0) + charged, 9)


def _at_budget(settings: Settings, spend: float) -> bool:
    """True only when a budget is set and reached.

    The page's slider offers 0. Treating it as "at budget" made zero a silent
    off switch for every mode; 0 means no budget.
    """
    budget = float(getattr(settings, "budget_session_usd", 0.0) or 0.0)
    if budget <= 0:
        return False
    return float(spend or 0.0) >= budget


def _find_turn(state: Any, turn_id: str) -> dict[str, Any] | None:
    for record in reversed(state.ledger):
        if isinstance(record, dict) and str(record.get("turn_id")) == turn_id:
            return record
    return None




_ADVICE_KEEP = 5
_SEEN_CWDS_MAX = 20


def _request_text(data: Mapping[str, Any]) -> str:
    for key in ("user_message", "request", "last_user_message"):
        text = str(data.get(key) or "").strip()
        if text:
            return text
    history = data.get("conversation_history")
    if isinstance(history, list):
        for message in reversed(history):
            if (
                isinstance(message, Mapping)
                and str(message.get("role") or "").strip().lower() == "user"
            ):
                return str(message.get("content") or "").strip()
    return ""


class CoachPlugin(CommandsMixin):
    """Stateful plugin instance: two tools, the /coach surface, and guards."""

    def __init__(
        self,
        *,
        llm: Any,
        store: Store,
        settings: Settings,
        primary_model: str = "",
        primary_provider: str = "",
        post: Callable[[str], Any] | None = None,
        inject: Callable[[str], Any] | None = None,
        surface: Callable[[], str] | str = lambda: "cli",
        cwd: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
        cost_fn: Callable[[str, Any, str], float] | None = None,
        guidance: Guidance | None = None,
        set_setting: Callable[..., Any] | None = None,
        settings_reader: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.llm = llm
        self.store = store
        self.settings = settings
        self.primary_model = primary_model
        self.primary_provider = primary_provider
        self.post = post
        self.inject = inject
        self.surface = surface
        self.cwd = cwd if cwd is not None else (lambda: os.getcwd())
        self.clock = clock
        self.set_setting = set_setting
        self.settings_reader = settings_reader
        self.guidance = guidance if guidance is not None else Guidance(_default_root())
        self.coach = Coach(
            llm,
            settings,
            primary_model=primary_model,
            primary_provider=primary_provider,
            cost_fn=cost_fn,
        )
        self._child_sessions: set[str] = set()
        self._queued: dict[tuple[str, str], None] = {}
        self._queued_lock = threading.Lock()
        # One entry per in-flight paid call, used to hold a cost floor against
        # the budget until the reply is priced.
        self._budget_inflight: dict[str, int] = {}
        self._tools_lock = threading.Lock()
        # Guards every store.load() -> mutate -> store.save() sequence, across
        # threads and across processes: the review worker and the host hooks
        # run on different threads, and the desktop app and a CLI session can
        # hold the same state file at once.
        self._store_lock = StoreLock(self.store.root / ".lock")
        # Per-session HEAD snapshot taken when the current turn started, so the
        # completion gate can diff the commits the turn produced. Snapshot at
        # turn start, never per model call: a mid-turn commit must stay inside
        # the range instead of moving the base past it.
        self._turn_base: dict[str, tuple[str, str]] = {}
        self._base_lock = threading.Lock()
        self._turn_tools: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._seen_cwds: set[str] = set()
        # Where coach/state.json lives; the store root's parent in production.
        self.home = self.store.root.parent
        self._paused = False

    def shutdown(self) -> None:
        """Stop the review worker when the host unloads the plugin."""
        worker = getattr(self, "worker", None)
        try:
            if worker is not None:
                try:
                    timeout = float(self.settings.worker_timeout_s)
                except (TypeError, ValueError):
                    timeout = 5.0
                worker.stop(timeout=min(max(1.0, timeout), 20.0))
        except Exception:
            log.debug("coach: worker stop failed", exc_info=True)

    def _remember_primary(self, data: Mapping[str, Any]) -> None:
        """Track the model the session is actually running.

        Config's default can differ from the model a chat was started with, and
        the hook payload is the only place the live value appears.
        """
        model = str(data.get("model") or "").strip()
        if not model:
            return
        self.primary_model = model
        self.coach.primary_model = model
        session_id = str(data.get("session_id") or "")
        if session_id:
            try:
                with self._store_lock:
                    state = self.store.load(session_id)
                    if state.last_primary_model != model:
                        state.last_primary_model = model
                        state.updated_at = self.clock()
                        self.store.save(state)
            except Exception:
                log.debug("coach: could not persist last_primary_model", exc_info=True)

    def _snapshot_turn_base(self, data: Mapping[str, Any]) -> None:
        """Remember the commit HEAD points at when a new turn starts.

        The completion gate diffs ``base..HEAD`` so work the agent already
        committed is still reviewable. A turn spans several ``pre_llm_call``
        firings (one per model call); only the first one for a turn may set the
        base, or a mid-turn commit would move it past the change being made.
        """
        session_id = str(data.get("session_id") or "")
        turn_id = str(data.get("turn_id") or "")
        if not session_id or not turn_id:
            return
        with self._base_lock:
            current = self._turn_base.get(session_id)
            if current is not None and current[0] == turn_id:
                return
        payload_cwd = str(data.get("cwd") or "").strip()
        try:
            own_cwd = str(self.cwd() or os.getcwd())
        except Exception:
            own_cwd = os.getcwd()
        if payload_cwd and os.path.abspath(payload_cwd) != os.path.abspath(own_cwd):
            try:
                commit = head(payload_cwd)
            except Exception:
                return
        else:
            commit = self.head_commit()
        if not commit:
            return
        with self._base_lock:
            current = self._turn_base.get(session_id)
            if current is not None and current[0] == turn_id:
                return
            if len(self._turn_base) > 64:
                # Hook firings can outlive session-end; keep the map bounded.
                self._turn_base.pop(next(iter(self._turn_base)), None)
            self._turn_base[session_id] = (turn_id, commit)

    def head_commit(self) -> str:
        """Return the working directory's HEAD commit; replaced in tests."""
        return head(str(self.cwd() or os.getcwd()))

    def refresh_settings(self) -> None:
        """Re-read settings so a page change applies without a plugin reload.

        The host builds the plugin once; the Desktop page writes config through
        a separate process. Without this, tools and hooks keep the startup copy
        and report "no coach model configured" until Hermes restarts.
        """
        reader = self.settings_reader
        if reader is None:
            return
        try:
            fresh = load_settings(reader())
        except Exception:
            return
        if fresh != self.settings:
            self.settings = fresh
            self.coach.settings = fresh

    def on_subagent_start(
        self, payload: Mapping[str, Any] | None = None, **kw: Any
    ) -> None:
        """Remember a child session so its tool calls are refused."""
        child = _child_id(_payload(payload, kw))
        if child:
            self._child_sessions.add(child)

    def on_subagent_stop(
        self, payload: Mapping[str, Any] | None = None, **kw: Any
    ) -> None:
        """Forget a child session once it has ended."""
        child = _child_id(_payload(payload, kw))
        if child:
            self._child_sessions.discard(child)

    def on_session_end(
        self, payload: Mapping[str, Any] | None = None, **kw: Any
    ) -> None:
        """Drop the oldest session files once a session closes."""
        data = _payload(payload, kw)
        session_id = str(data.get("session_id") or "")
        if session_id:
            # Buffered tool cards for the closed session will never be read.
            with self._tools_lock:
                for key in [k for k in self._turn_tools if k[0] == session_id]:
                    del self._turn_tools[key]
            with self._base_lock:
                self._turn_base.pop(session_id, None)
            with self._queued_lock:
                for key in [k for k in self._queued if k[0] == session_id]:
                    self._queued.pop(key, None)
        try:
            self.store.prune()
        except Exception:
            log.warning("coach: session-end prune failed", exc_info=True)

    def status(self, session_id: str = "current") -> dict[str, Any]:
        """Report configuration, modes, and one session's spend."""
        self.refresh_settings()
        try:
            state = self.store.load(str(session_id or "current"))
        except Exception:
            state = self.store.load("current")
        # spend_total_usd is profile-wide: sum every on-disk session plus the
        # in-memory state when it is not yet the file's copy.
        try:
            all_states = self.store.states_on_disk()
        except Exception:
            all_states = []
        spend_sessions = {
            session.session_id: round(session.spend_usd, 6)
            for session in all_states
        }
        spend_total = sum(session.spend_usd for session in all_states)
        if state.session_id not in spend_sessions:
            spend_sessions[state.session_id] = round(state.spend_usd, 6)
            spend_total += state.spend_usd
        return {
            "configured": self.settings.is_configured(),
            "coach_model": _model_dict(self.settings.coach_model),
            "active_model": _model_dict(self.settings.model_for("active")),
            "review_model": _model_dict(self.settings.model_for("review")),
            "consult_model": _model_dict(self.settings.model_for("consult")),
            "active_enabled": self.settings.active_enabled,
            "review_enabled": self.settings.review_enabled,
            "decide_enabled": self.settings.decide_enabled,
            "decide_backend": self.settings.decide_backend,
            "spend_usd": round(state.spend_usd, 6),
            "spend_total_usd": round(spend_total, 6),
            "spend_sessions": spend_sessions,
            "budget_usd": self.settings.budget_session_usd,
            "budget_per_session_usd": self.settings.budget_session_usd,
            "budget_on_exceed": self.settings.budget_on_exceed,
            "allow_in_subagents": self.settings.allow_in_subagents,
            "pending_notes": len(state.pending),
            "sessions": len(self.store.sessions_on_disk()),
            "review_skips": int(state.calls.get("review_skips") or 0),
            "decide_unpriced": int(state.calls.get("decide_unpriced") or 0),
            "last_error": state.last_error,
            "paused": self._paused,
        }


    def _debug(self, message: str) -> None:
        if self.settings.debug_log_decisions:
            log.debug("coach: %s", message)

    def _prompt_call(
        self, state: Any, system: str, guidance: str, question: str | None
    ) -> list[Any]:
        """One bounded coach prompt; the ledger caps live here alone."""
        return build_messages(
            system=system,
            guidance=guidance,
            state=state,
            question=question,
            max_tokens=self.settings.ledger_max_tokens,
            tool_result_chars=self.settings.ledger_max_tool_result_chars,
            recent_turns=self.settings.ledger_max_recent_turns,
            note_max_chars=self.settings.ledger_note_max_chars,
        )

    def deliver(
        self,
        finding: Finding | None,
        *,
        session_id: str,
        turn_id: str,
        allow_corrective: bool = True,
    ) -> None:
        """Route one finding to a card, a pending note, or an injection.

        ``allow_corrective=False`` is for the completion gate: it has its own
        single-hold authority, so a delivery-policy injection there would be a
        second, unbudgeted way to start a turn. The finding still shows as a
        card and still reaches the model as a note on the next turn.
        """
        if finding is None:
            return
        with self._store_lock:
            state = self.store.load(session_id)
            counters = TurnCounters(state.calls)
            holds_used = counters.get("corrective", turn_id)
            decision = route(
                finding,
                self.settings,
                surface=self._surface_kind(),
                holds_used=holds_used,
            )
            if decision == "corrective" and not allow_corrective:
                decision = "ambient"
            self._debug(
                f"deliver session={session_id} turn={turn_id} "
                f"decision={decision} surface={self._surface_kind()} "
                f"turn_holds={holds_used}"
            )
            if decision == "drop":
                return
            if decision == "corrective":
                # Reserve the slot in the same critical section as the check,
                # so two concurrent deliveries cannot both pass the cap.
                counters.reserve("corrective", turn_id)
                state.updated_at = self.clock()
                self.store.save(state)
        self.card(finding)
        injected = bool(
            decision == "corrective" and self._inject(format_corrective(finding))
        )
        with self._store_lock:
            # Re-load: the card post and injection above are host calls that
            # may run while another thread records a turn.
            state = self.store.load(session_id)
            state.updated_at = self.clock()
            if injected:
                # Per turn, because the setting is "per request"; state.holds
                # stays as the session-lifetime telemetry /coach status shows.
                state.holds += 1
                self.store.save(state)
                return
            if decision == "corrective":
                # The injection did not start a turn: release the reservation.
                TurnCounters(state.calls).release("corrective", turn_id)

            ttl = max(0, int(self.settings.delivery_note_ttl_turns))
            note_text = format_ambient(finding, turn_id)
            state.pending = [
                note
                for note in state.pending
                if isinstance(note, Mapping)
                and _as_turn(note.get("expires_at_turn")) >= state.note_seq
                # One note per (turn, content): Active and the completion gate
                # can both flag the same turn, and the model should read it
                # once. A same-title finding with different advice is a second
                # note, not a replacement.
                and not (
                    str(note.get("turn_id") or "") == str(turn_id or "")
                    and str(note.get("text") or "") == note_text
                )
            ]
            state.pending.append(
                {
                    "turn_id": str(turn_id or ""),
                    "expires_at_turn": int(state.note_seq) + ttl,
                    "text": note_text,
                    "severity": finding.severity,
                    "title": finding.title,
                    "body": finding.body,
                    "evidence": finding.evidence,
                    "fix": finding.fix,
                }
            )
            state.updated_at = self.clock()
            try:
                self.store.save(state)
            except Exception:
                log.warning(
                    "coach: could not persist note %r", finding.title, exc_info=True
                )
                if decision == "corrective":
                    # The reservation was persisted, the note was not: give the
                    # slot back rather than spending the turn's corrective cap
                    # on a delivery nobody can read.
                    try:
                        self._reload_and_save(
                            session_id,
                            lambda current: TurnCounters(current.calls).release(
                                "corrective", turn_id
                            ),
                        )
                    except Exception:
                        log.warning("coach: could not release corrective slot")

    def _remember_cwd(self) -> None:
        """Add the working directory to ``coach/state.json`` for project scope.

        The dashboard allows project guidance only under a directory this
        plugin has seen; nothing wrote that list, so every real project was
        refused with ``cwd_not_registered`` and the feature could not work.
        Best-effort and once per directory per process.
        """
        try:
            cwd = str(self.cwd() or "").strip()
            if not cwd or not os.path.isabs(cwd) or cwd in self._seen_cwds:
                return
            path = self.home / "state.json"
            with self._store_lock:
                data: dict[str, Any] = {}
                try:
                    if path.is_file():
                        loaded = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(loaded, Mapping):
                            data = dict(loaded)
                except (OSError, ValueError):
                    data = {}
                seen = data.get("seen_cwds")
                seen = [str(item) for item in seen] if isinstance(seen, list) else []
                if cwd not in seen:
                    data["seen_cwds"] = [*seen, cwd][-_SEEN_CWDS_MAX:]
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    # mkstemp + replace, like Store.save: a fixed tmp name lets
                    # two writers (desktop and CLI) drop each other's update.
                    descriptor, temporary = tempfile.mkstemp(
                        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
                    )
                    try:
                        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                            json.dump(data, handle)
                        os.chmod(temporary, 0o600)
                        os.replace(temporary, path)
                    except BaseException:
                        try:
                            os.unlink(temporary)
                        except OSError:
                            pass
                        raise
            self._seen_cwds.add(cwd)
        except Exception:
            return

    def on_pre_llm_call(
        self, payload: Mapping[str, Any] | None = None, **kw: Any
    ) -> dict[str, str] | None:
        """Return pending ambient notes, plus opt-in pre-turn guidance."""
        try:
            self.refresh_settings()
            data = _payload(payload, kw)
            self._remember_primary(data)
            self._remember_cwd()
            self._snapshot_turn_base(data)
            session_id = str(data.get("session_id") or "current")
            turn_id = str(data.get("turn_id") or "current")
            user_text = _request_text(data)
            with self._store_lock:
                state = self.store.load(session_id)
                if not state.pending:
                    # Nothing to deliver and nothing to expire: rewriting the
                    # whole session file on every model call was measurable
                    # hook cost for no state change.
                    pending_block = ""
                else:
                    state.note_seq = _as_turn(state.note_seq) + 1

                    delivered: list[str] = []
                    for note in state.pending:
                        if not isinstance(note, Mapping):
                            continue
                        if _as_turn(note.get("expires_at_turn")) < state.note_seq:
                            continue
                        text = str(note.get("text") or "").strip()
                        if text:
                            delivered.append(text)
                    state.pending = []
                    state.updated_at = self.clock()
                    self.store.save(state)
                    pending_block = "\n".join(delivered)

            parts = [pending_block] if pending_block else []
            try:
                timing = str(
                    getattr(self.settings, "active_timing", "post") or "post"
                )
                if user_text and timing in ("pre", "both"):
                    with self._store_lock:
                        fresh = self.store.load(session_id)
                    if self.should_pre_review(fresh, self.settings, user_text):
                        extra = self._pre_turn_guidance(
                            session_id, turn_id, user_text, fresh
                        )
                        if extra:
                            parts.append(extra)
            except Exception:
                pass

            if not parts:
                return None
            return {"context": "\n".join(parts)}
        except Exception:
            return None

    def on_post_tool_call(
        self, payload: Mapping[str, Any] | None = None, **kw: Any
    ) -> None:
        """Buffer one tool card for the turn Active may review."""
        if not self.settings.active_enabled:
            return
        data = _payload(payload, kw)
        session_id = str(data.get("session_id") or "current").strip()
        turn_id = str(data.get("turn_id") or "current").strip()
        args = data.get("args")
        if not isinstance(args, Mapping):
            args = {}
        text = _result_text(data.get("result"))
        status = str(data.get("status") or ("error" if data.get("error") else "ok"))
        card = tool_card(
            str(data.get("tool_name") or data.get("name") or "tool"),
            _tool_arg(args),
            status,
            len(text),
            text,
            max_chars=self.settings.ledger_max_tool_result_chars,
        )
        with self._tools_lock:
            buffered = self._turn_tools.setdefault((session_id, turn_id), [])
            buffered.append(card)
            if len(buffered) > 40:
                # A retry storm must not hold every card from one turn until
                # post_llm_call pops the key.
                del buffered[:-40]
            if len(self._turn_tools) > 500:
                # Hooks fire per tool call; if session end is missed, drop the
                # oldest buffered cards instead of growing without bound.
                for stale in list(self._turn_tools)[: len(self._turn_tools) - 500]:
                    del self._turn_tools[stale]
        # No per-call state write: the counter this used to increment was
        # never read, and the write made every tool call pay ~19 ms of JSON
        # rewrite under the store lock (on_post_llm_call persists the turn).

    def on_post_llm_call(
        self, payload: Mapping[str, Any] | None = None, **kw: Any
    ) -> None:
        """Snapshot a finished turn and queue one Active review when due."""
        self.refresh_settings()
        data = _payload(payload, kw)
        self._remember_primary(data)
        session_id = str(data.get("session_id") or "current").strip()
        turn_id = str(data.get("turn_id") or "current").strip()
        with self._tools_lock:
            tools = self._turn_tools.pop((session_id, turn_id), [])
        if not self.settings.active_enabled and not self.settings.review_enabled:
            return
        try:
            with self._store_lock:
                seen = self.store.load(session_id)
                message = str(data.get("user_message") or data.get("user") or "").strip()
                if message:
                    seen.last_user_message = message[:4000]
                    seen.updated_at = self.clock()
                    self.store.save(seen)
        except Exception:
            pass
        turn = {
            "turn_id": turn_id,
            "user": str(data.get("user_message") or data.get("user") or ""),
            "assistant": str(
                data.get("assistant_response") or data.get("assistant") or ""
            ),
            "tools": tools,
            "cwd": str(self.cwd() or os.getcwd()),
        }
        turn["digest"] = _turn_digest(turn)
        try:
            with self._store_lock:
                state = self.store.load(session_id)
                _record_turn(state, turn)
                worker = getattr(self, "worker", None)
                due = worker is not None and self.should_review(state, turn, self.settings)
                state.updated_at = self.clock()
                # Persist before submitting: the worker loads the state itself, and
                # a submit first means it can find no turn and drop the review.
                self.store.save(state)
            if due and worker is not None:
                key = (session_id, turn_id)
                stalled = False
                with self._queued_lock:
                    for stale in [
                        queued
                        for queued in self._queued
                        if queued[0] == session_id and queued[1] != turn_id
                    ]:
                        del self._queued[stale]
                    submitted = False
                    try:
                        submitted = bool(worker.submit(session_id, turn_id))
                    except Exception:
                        submitted = False
                    try:
                        evicted = worker.pop_evicted()
                    except Exception:
                        evicted = None
                    if evicted is not None:
                        self._queued.pop((str(evicted[0]), str(evicted[1])), None)
                    if key not in self._queued and submitted:
                        self._queued[key] = None
                    stalled = not submitted
                if stalled:
                    self._record_skip(session_id, "worker_stalled")
        except Exception:
            pass

    def should_review(
        self,
        state: Any,
        turn: Mapping[str, Any],
        settings: Settings,
    ) -> bool:
        """Decide whether one finished turn earns an Active review."""
        if not settings.active_enabled or self._paused:
            return False
        if str(getattr(settings, "active_timing", "post") or "post") == "pre":
            return False
        if self.coach.check("active"):
            return False
        if settings.budget_on_exceed in {"soft", "hard"} and _at_budget(
            settings, state.spend_usd
        ):
            return False
        cadence = settings.active_cadence
        tools = turn.get("tools") if isinstance(turn, Mapping) else None
        if cadence == "tool_turns_only" and not tools:
            return False
        if cadence == "every_n":
            every = max(1, int(settings.active_every_n))
            if _turns_since_review(state) < every:
                return False
        digest = _turn_digest(turn)
        if digest and digest == _last_reviewed_digest(state):
            return False
        return True

    def should_pre_review(
        self,
        state: Any,
        settings: Settings,
        user_text: str,
    ) -> bool:
        """Decide whether the coming turn earns pre-turn Active guidance."""
        if not settings.active_enabled or self._paused:
            return False
        if str(getattr(settings, "active_timing", "post") or "post") not in (
            "pre",
            "both",
        ):
            return False
        if self.coach.check("active"):
            return False
        if settings.budget_on_exceed in {"soft", "hard"} and _at_budget(
            settings, state.spend_usd
        ):
            return False
        text = str(user_text or "").strip()
        if not text:
            return False
        digest = _pre_digest(text)
        if digest and digest == _last_pre_digest(state):
            return False
        if settings.active_cadence == "every_n":
            # Reset scope is the session ledger: the first guided turn is
            # always due, then one guidance per N turns.
            every = max(1, int(settings.active_every_n))
            guided_any = any(
                isinstance(record, Mapping) and record.get("pre_guidance")
                for record in state.ledger
            )
            if guided_any and _turns_since_pre_guidance(state) < every - 1:
                return False
        # tool_turns_only maps to every_turn pre-turn: no tools exist yet.
        return True

    def _pre_turn_guidance(
        self,
        session_id: str,
        turn_id: str,
        user_text: str,
        state: Any = None,
    ) -> str:
        """Run one bounded pre-turn guidance call; empty string on any miss.

        The charge and the provenance land in one atomic commit, and the
        payload is built from the saved record — partial guidance never
        reaches the turn. A structured finding earns a card for visibility,
        but never a pending note: the guidance itself is already injected.
        """
        sid = str(session_id or "current")
        tid = str(turn_id or "current")
        text = str(user_text or "").strip()
        if not text:
            return ""
        try:
            cwd = str(self.cwd() or os.getcwd())
            guidance = (
                self.guidance.system_block(cwd)
                if self.settings.guidance_enabled
                else ""
            )
            if state is None:
                with self._store_lock:
                    state = self.store.load(sid)
            # Reuse the prompt builder so ledger/user text stays data, never
            # directives — no raw string concat on this path.
            messages = self._prompt_call(state, _PRE_SYSTEM, guidance, text)
            reply = self.coach.ask(
                messages,
                mode="active",
                structured=False,
                timeout=min(float(self.settings.worker_timeout_s), 10.0),
            )
        except Exception as exc:  # noqa: BLE001 - fail-open is the contract
            self._record_skip(sid, f"pre-turn crashed: {type(exc).__name__}")
            return ""
        body = str(getattr(reply, "text", "") or "").strip()
        if reply.error or not body:
            self._record_skip(sid, reply.error or "empty_guidance")
            return ""
        capped = body[:500]
        digest = _pre_digest(text)
        reason = str(self.settings.active_cadence)

        def commit(state: Any) -> None:
            _charge(state, reply.cost_usd)
            _record_turn(
                state,
                {
                    "turn_id": tid,
                    "user": text[:4000],
                    "assistant": "",
                    "tools": [],
                    "cwd": str(self.cwd() or os.getcwd()),
                    "digest": digest,
                    "pre_guidance": capped,
                    "pre_reason": reason,
                    "pre_digest": digest,
                },
            )
            state.last_error = ""

        # A paid call must not vanish silently: retry once, then verify the
        # record actually persisted before injecting anything.
        self._save_with_retry(sid, commit, what="pre-turn charge")
        if reply.finding is not None:
            self.card(reply.finding)
        try:
            with self._store_lock:
                record = _find_turn(self.store.load(sid), tid)
            saved = str((record or {}).get("pre_guidance") or "")
        except Exception:
            saved = ""
        if not saved:
            self._record_skip(sid, "pre-turn save failed")
            return ""
        return saved

    def run_review(self, session_id: str, turn_id: str) -> None:
        """Run one Active review; every outcome is swallowed and recorded."""
        self.refresh_settings()
        sid = str(session_id or "current")
        tid = str(turn_id or "current")
        state = None
        try:
            with self._store_lock:
                state = self.store.load(sid)
                if self._paused:
                    return
                turn = _find_turn(state, tid)
                if turn is None:
                    return
                over_budget = (
                    self.settings.budget_on_exceed in {"soft", "hard"}
                    and _at_budget(self.settings, state.spend_usd)
                )
                if over_budget:
                    return
                digest = str(turn.get("digest") or _turn_digest(turn))
                cwd = str(turn.get("cwd") or self.cwd() or os.getcwd())
            # Prompt assembly reads guidance files: keep it out of the critical
            # section so hooks are not serialised behind file I/O.
            guidance = (
                self.guidance.system_block(cwd) if self.settings.guidance_enabled else ""
            )
            messages = self._prompt_call(state, _SYSTEM, guidance, None)
            reply = self.coach.ask(
                messages,
                mode="active",
                structured=True,
                timeout=float(self.settings.worker_timeout_s),
            )
            def commit(state: Any) -> None:
                turn = _find_turn(state, tid)
                _charge(state, reply.cost_usd)
                if turn is not None:
                    turn["reviewed"] = True
                    turn["digest"] = digest
                state.last_error = reply.error

            self._reload_and_save(sid, commit)
            self._log_review(sid, tid, reply)
            self._debug(
                f"active review {sid}/{tid} error={reply.error!r} "
                f"finding={reply.finding.severity if reply.finding else None} "
                f"cost={reply.cost_usd}"
            )
            if not reply.error:
                self.deliver(reply.finding, session_id=sid, turn_id=tid)
        except Exception as exc:  # noqa: BLE001 - fail-open is the contract
            self._record_skip(sid, f"review crashed: {type(exc).__name__}")
        finally:
            with self._queued_lock:
                self._queued.pop((sid, tid), None)

    def _log_review(self, session_id: str, turn_id: str, reply: Any) -> None:
        """Append one JSONL line per attempted review; never raise."""
        finding = reply.finding
        try:
            record = {
                "kind": "review",
                "session_id": session_id,
                "turn_id": turn_id,
                "severity": finding.severity if finding is not None else "none",
                "title": finding.title if finding is not None else "",
                "body": finding.body if finding is not None else "",
                "evidence": finding.evidence if finding is not None else "",
                "fix": finding.fix if finding is not None else "",
                "input_tokens": int(getattr(reply.usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(reply.usage, "output_tokens", 0) or 0),
                "cost_usd": reply.cost_usd,
                "cost_estimated": bool(
                    getattr(reply, "cost_estimated", False)
                ),
                "provider": reply.provider,
                "model": reply.model,
                "error": reply.error,
                "at": _now_iso(),
            }
            append_log(self.store.root, session_id, record)
        except Exception:
            log.debug("coach: review log write failed", exc_info=True)

    def _log_decision(
        self,
        session_id: str,
        turn_id: str,
        decision: Any,
        backend: str,
        question: str = "",
        options: Any = (),
    ) -> None:
        """Append one JSONL line per decision; never raise."""
        try:
            record = {
                "kind": "decide",
                "session_id": session_id,
                "turn_id": turn_id,
                # What the decision was about and which choices existed: a
                # rationale alone cannot be re-read later.
                "question": str(question or "").strip()[:500],
                "options": [str(option).strip()[:120] for option in (options or ())][:8],
                "decision": decision.decision,
                "confidence": decision.confidence,
                "gate": decision.gate,
                "rationale": str(decision.rationale or "")[:_RATIONALE_MAX],
                "cost_usd": decision.cost_usd,
                "backend": backend,
                "at": _now_iso(),
            }
            append_log(self.store.root, session_id, record)
        except Exception:
            log.debug("coach: decision log write failed", exc_info=True)

    def on_pre_verify(
        self, payload: Mapping[str, Any] | None = None, **kw: Any
    ) -> dict[str, str] | None:
        """Gate a completion claim on one bounded review; fail open."""
        try:
            return self._pre_verify(_payload(payload, kw))
        except Exception:
            log.warning("coach: pre_verify gate failed open", exc_info=True)
            try:
                data = _payload(payload, kw)
                self._record_skip(
                    str(data.get("session_id") or "current"),
                    "gate skipped: exception",
                )
            except Exception:
                pass
            return None

    def _pre_verify(self, data: Mapping[str, Any]) -> dict[str, str] | None:
        started = time.monotonic()
        self.refresh_settings()
        if not self.settings.review_enabled or self._paused:
            return None
        paths = data.get("changed_paths")
        session_id = str(data.get("session_id") or "current").strip()
        turn_id = str(data.get("turn_id") or "current").strip()
        if not isinstance(paths, (list, tuple)):
            self._record_skip(session_id, "gate skipped: no changed-path report")
            return None
        changed = [str(path).strip() for path in paths if str(path or "").strip()]
        if not changed:
            self._record_skip(session_id, "gate skipped: empty changed-path report")
            return None
        with self._store_lock:
            state = self.store.load(session_id)
            if (
                self.settings.budget_on_exceed in {"soft", "hard"}
                and _at_budget(self.settings, state.spend_usd)
            ):
                return None

            used = TurnCounters(state.calls).get("review_holds", turn_id)
            # The cap is on HOLDS, not reviews: the claim that answers a held
            # blocker must still be reviewed, or the fix is never checked.
            can_hold = used < max(0, int(self.settings.review_max_holds))

        cwd = str(data.get("cwd") or self.cwd())
        base = ""
        with self._base_lock:
            entry = self._turn_base.get(session_id)
            if entry is not None and str(entry[0]) == turn_id:
                base = entry[1]
        evidence = self.collect_evidence(cwd, changed, base=base)
        if not evidence.strip():
            self._record_skip(session_id, "gate skipped: no evidence collected")
            return None
        left = _GATE_BUDGET_S - (time.monotonic() - started) - 1.5
        if left < _GATE_MIN_REVIEW_S:
            # Evidence collection already ate the window. A review started now
            # would be killed by the host and take the hook down with it, so
            # skip it: no verdict is better than a silenced gate.
            log.info("coach: gate skipped, %.1fs left after evidence", left)
            self._record_skip(
                session_id, f"gate skipped: {left:.1f}s left after evidence"
            )
            return None
        claim_cap = max(2000, int(self.settings.review_evidence_max_chars))
        request_text = (_request_text(data) or state.last_user_message or "")[:claim_cap]
        claim_text = str(data.get("final_response") or "")[:claim_cap]
        finding = self.review_now(
            session_id=session_id,
            turn_id=turn_id,
            request=request_text,
            claim=claim_text,
            evidence=evidence,
            cwd=cwd,
            timeout=min(_REVIEW_TIMEOUT_S, left),
        )
        if finding is None:
            return None
        if (
            finding.severity == "blocker"
            and self.settings.review_authority == "hold_once"
            and can_hold
        ):
            # Re-check the cap under the lock: the decision came from a
            # snapshot taken before the (long) review, so a concurrent gate
            # firing for the same turn may have used the last hold meanwhile.
            granted = False
            with self._store_lock:
                state = self.store.load(session_id)
                cap = max(0, int(self.settings.review_max_holds))
                counters = TurnCounters(state.calls)
                if counters.get("review_holds", turn_id) < cap:
                    counters.reserve("review_holds", turn_id)
                    state.updated_at = self.clock()
                    self.store.save(state)
                    granted = True
            if granted:
                # The user sees the blocker too, not only the agent.
                self.card(finding)
                return {"action": "continue", "message": _hold_message(finding)}

        # Everything else reaches the model as a note on the next turn, through
        # the same path Active uses. Without this a concern is a card the model
        # never reads. allow_corrective=False: the gate has its own hold slot,
        # so delivery policy must not open a second, uncapped turn from here.
        self.deliver(
            finding,
            session_id=session_id,
            turn_id=turn_id,
            allow_corrective=False,
        )
        return None

    def collect_evidence(self, cwd: str, paths: list[str], base: str = "") -> str:
        """Return bounded diff evidence; replaced in tests."""
        return collect(
            cwd,
            paths,
            base=base,
            max_chars=self.settings.review_evidence_max_chars,
        )

    def review_now(
        self,
        *,
        session_id: str,
        turn_id: str,
        request: str,
        claim: str,
        evidence: str,
        cwd: str = "",
        timeout: float = _REVIEW_TIMEOUT_S,
    ) -> Finding | None:
        """Run the single structured Review call and record its spend."""
        state = self.store.load(session_id)
        try:
            root = str(cwd or self.cwd() or os.getcwd())
            guidance = (
                self.guidance.system_block(root)
                if self.settings.guidance_enabled
                else ""
            )
            question = (
                "<coach-request>\n"
                f"{strip_delimited(request or '(not available)', 'coach-request')}\n"
                "</coach-request>\n\n"
                "<coach-claim>\n"
                f"{strip_delimited(claim or '(none)', 'coach-claim')}\n"
                "</coach-claim>\n\n"
                "<coach-evidence>\n"
                f"{strip_delimited(evidence or '(no diff)', 'coach-evidence')}\n"
                "</coach-evidence>"
            )
            messages = self._prompt_call(state, _REVIEW_SYSTEM, guidance, question)
            reply = self.coach.ask(
                messages,
                mode="review",
                structured=True,
                timeout=float(timeout),
            )
            def commit(state: Any) -> None:
                _charge(state, reply.cost_usd)
                state.last_error = reply.error

            self._save_with_retry(session_id, commit, what="review charge")
            self._log_review(session_id, turn_id, reply)
            if reply.error:
                return None
            return reply.finding
        except Exception as exc:  # noqa: BLE001 - fail-open is the contract
            self._record_skip(session_id, f"review failed: {type(exc).__name__}")
            return None

    def _record_skip(self, session_id: str, reason: str) -> None:
        """Surface a swallowed skip: counter + last_error, best effort.

        The plugin is fail-open by contract — no error stops the host — but
        every oversight skip must leave a trace the user can find in
        ``/coach status`` instead of vanishing into a logger.
        """
        try:
            with self._store_lock:
                state = self.store.load(session_id)
                state.calls["review_skips"] = (
                    int(state.calls.get("review_skips") or 0) + 1
                )
                state.last_error = str(reason)[:200]
                state.updated_at = self.clock()
                self.store.save(state)
        except Exception:
            pass

    def _reload_and_save(self, session_id: str, commit: Callable[[Any], Any]) -> Any:
        """Apply ``commit`` onto a fresh reload under the store lock, then save.

        Every reload-under-lock site here serves one contract: a model call
        ran outside the lock, so the copy used for the prompt may be stale;
        applying onto it would silently discard concurrent host updates.
        The callback mutates the reloaded state and returns the caller's
        value (``None`` on the plain path); the save is ours.
        """
        with self._store_lock:
            state = self.store.load(session_id)
            result = commit(state)
            state.updated_at = self.clock()
            self.store.save(state)
            return result

    def _save_with_retry(self, session_id: str, commit: Callable[[Any], Any], *, what: str) -> Any:
        """Reload-and-save, retrying once; a paid call must not vanish silently.

        A failure here happens after the provider replied (or after a note was
        posted), so the side effect already happened. Two attempts, then an
        error log the operator can find; the caller still returns its result.
        """
        for attempt in (1, 2):
            try:
                return self._reload_and_save(session_id, commit)
            except Exception:
                log.warning(
                    "coach: %s save failed (attempt %d)", what, attempt, exc_info=True
                )
        log.error("coach: %s not persisted; spend may be under-counted", what)
        return None

    def card(self, finding: Finding) -> None:
        """Post the user-visible card for a finding, when cards are enabled."""
        if self.settings.delivery_show_cards:
            self._post(format_card(finding))

    def card_text(self, text: str) -> None:
        """Post a user-visible card for advice that is not a finding."""
        if self.settings.delivery_show_cards:
            self._post(text)

    def _post(self, text: str) -> None:
        if self.post is None:
            return
        try:
            self.post(text)
        except Exception:
            pass

    def _inject(self, text: str) -> bool:
        if self.inject is None:
            return False
        try:
            return self.inject(text) is not False
        except Exception:
            return False

    def tool_ask(self, args: dict, **kw: Any) -> str:
        """Answer one consultation as a JSON string; never raise."""
        return self._tool(self._ask, args, kw)

    def _tool(
        self,
        handler: Callable[[Mapping[str, Any]], dict[str, Any]],
        args: Mapping[str, Any],
        kw: Mapping[str, Any],
    ) -> str:
        """Run one tool handler: host ids in, JSON out, never raise."""
        self.refresh_settings()
        try:
            return json.dumps(handler(_with_host_ids(args, kw)))
        except Exception as exc:  # fail-open by contract
            return json.dumps(
                _error(f"internal_error: {type(exc).__name__}"),
                ensure_ascii=False,
            )

    def _call_floor(self) -> float:
        """A conservative per-call cost floor from the fallback price.

        The input side is unknown before the call, so this is a floor rather
        than a price: it stops a burst of calls from all passing the same
        pre-call spend figure, and the post-call charge closes the rest.
        """
        price = as_finite_float(self.settings.fallback_price_per_mtok, 0.0)
        tokens = as_finite_float(self.settings.max_reply_tokens, 0.0)
        return max(0.0, price * tokens / 1_000_000)

    def _inflight_estimate(self, session_id: str) -> float:
        return self._budget_inflight.get(session_id, 0) * self._call_floor()

    def _enter_budget(self, session_id: str) -> None:
        self._budget_inflight[session_id] = (
            self._budget_inflight.get(session_id, 0) + 1
        )

    def _leave_budget(self, session_id: str) -> None:
        remaining = self._budget_inflight.get(session_id, 0) - 1
        if remaining > 0:
            self._budget_inflight[session_id] = remaining
        else:
            self._budget_inflight.pop(session_id, None)

    def _reserve(
        self,
        session_id: str,
        turn_id: Any,
        namespace: str,
        *,
        budget_modes: set[str],
        limit: int,
    ) -> tuple[Any, str, dict[str, Any] | None]:
        """Load, budget-check, cap-check and reserve one call slot under the lock.

        Returns the reserved state, the effective turn id and an error envelope
        (or None). A refusal reserves nothing; a reservation is saved before the
        caller makes the call, so two concurrent calls cannot share one slot.
        """
        tid = str(turn_id or "current")
        with self._store_lock:
            state = self.store.load(session_id)
            if session_id == "current":
                # An id-less dispatch shares one state bucket with every other
                # id-less call; record that it happened instead of hiding it.
                state.calls["id_fallbacks"] = (
                    int(state.calls.get("id_fallbacks") or 0) + 1
                )
            if (
                self.settings.budget_on_exceed in budget_modes
                and _at_budget(
                    self.settings,
                    state.spend_usd + self._inflight_estimate(session_id),
                )
            ):
                return state, tid, _error("budget_exhausted")
            counters = TurnCounters(state.calls)
            if counters.get(namespace, tid) >= limit:
                return state, tid, _error("call_cap")
            counters.reserve(namespace, tid)
            self._enter_budget(session_id)
            state.updated_at = self.clock()
            self.store.save(state)
            return state, tid, None

    def _ask(self, args: Mapping[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return _error("question_required")
        if len(question) > _QUESTION_MAX:
            return _error("question_too_long")
        raw_context = str(args.get("context") or "")
        if len(raw_context) > _CONTEXT_MAX:
            return _error("context_too_long")

        intent = str(args.get("intent") or "advice").strip().lower()
        if intent not in _INTENTS:
            intent = "advice"

        session_id = str(args.get("session_id") or "current").strip()
        if not self.settings.allow_in_subagents and self._is_child(session_id):
            return _error("child_session")

        if not self.settings.is_configured():
            return _error(_error_text("unconfigured"))
        # Consult is deliberately stricter than Review/Active/Decide: the user
        # asked for it, so only a hard stop refuses it.
        state, turn_id, error = self._reserve(
            session_id,
            args.get("turn_id"),
            "consult",
            budget_modes={"hard"},
            limit=self.settings.consult_max_calls_per_turn,
        )
        if error is not None:
            return error
        if state.last_primary_model:
            self.coach.primary_model = state.last_primary_model
        try:
            prompt = question
            context = str(args.get("context") or "").strip()[:_CONTEXT_MAX]
            if context:
                prompt = f"{question}\n\nContext:\n{context}"
            # Session-aware project dir: the model's working directory may
            # differ from this process's cwd in multi-session hosts.
            # Model-supplied paths are never trusted for the working directory.
            cwd = str(self.cwd() or os.getcwd())
            guidance = (
                self.guidance.system_block(cwd)
                if self.settings.guidance_enabled
                else ""
            )
            messages = self._prompt_call(state, _SYSTEM, guidance, prompt)
            reply = self.coach.ask(
                messages,
                mode="consult",
                structured=False,
                timeout=float(self.settings.worker_timeout_s),
            )
        except Exception:
            # Nothing answered the user: release the reservation so a provider
            # crash -- or a failure while assembling the prompt (guidance read,
            # cwd probe) -- does not burn the turn's consult budget.
            def release(current: Any) -> None:
                TurnCounters(current.calls).release("consult", turn_id)

            try:
                self._reload_and_save(session_id, release)
            except Exception:
                log.warning("coach: could not release consult reservation", exc_info=True)
            self._leave_budget(session_id)
            raise
        committed: list[Any] = []

        def commit(state: Any):
            committed.append(state)
            _charge(state, reply.cost_usd)
            self._leave_budget(session_id)
            if reply.error:
                state.last_error = reply.error
                return _error(_error_text(reply.error), spend_usd=state.spend_usd)
            append_turn(
                state,
                turn_id=turn_id,
                user=prompt,
                assistant=reply.text,
                tools=[],
            )
            entry = {
                "question": question,
                "text": reply.text,
                "intent": intent,
                "provider": reply.provider,
                "model": reply.model,
                "at": _now_iso(),
            }
            state.advice = [*state.advice, entry][-_ADVICE_KEEP:]
            state.last_advice = entry

        error = self._save_with_retry(session_id, commit, what="consult charge")
        if error is not None:
            return error
        state = committed[0] if committed else self.store.load(session_id)
        # Transparency: the user sees the advice even when the agent only
        # summarises it (or not at all) in its reply.
        self.card_text(
            _advice_card(reply.text, intent, reply.provider, reply.model)
        )
        return {
            "ok": True,
            "advice": reply.text,
            "intent": intent,
            "provider": reply.provider,
            "model": reply.model,
            "cost_usd": reply.cost_usd,
            "spend_usd": state.spend_usd,
            "calls_this_turn": TurnCounters(state.calls).get("consult", turn_id),
            "error": "",
        }

    def tool_decide(self, args: dict, **kw: Any) -> str:
        """Resolve one choice as a JSON string; never raise."""
        return self._tool(self._decide, args, kw)

    def _decide(self, args: Mapping[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return _error("question_required")
        if len(question) > _QUESTION_MAX:
            return _error("question_too_long")

        raw_options = args.get("options")
        if not isinstance(raw_options, (list, tuple)):
            return _error("options_required")
        options = [str(option).strip()[:_OPTION_MAX] for option in raw_options]
        if not _OPTIONS_MIN <= len(options) <= _OPTIONS_MAX:
            return _error("options_count")
        if any(not option for option in options) or len(set(options)) != len(options):
            return _error("options_invalid")

        if not self.settings.decide_enabled:
            return _error("decide_disabled")

        session_id = str(args.get("session_id") or "current").strip()
        if not self.settings.allow_in_subagents and self._is_child(session_id):
            return _error("child_session")

        _, turn_id, error = self._reserve(
            session_id,
            args.get("turn_id"),
            "decide",
            budget_modes={"soft", "hard"},
            limit=self.settings.decide_max_calls_per_turn,
        )
        if error is not None:
            return error

        backend = self.settings.decide_backend
        if backend == "jev-openrouter":
            api_key = _get_secret("OPENROUTER_API_KEY").strip()
        elif backend == "jev-direct":
            api_key = (
                self.settings.decide_typesafe_api_key
                or _get_secret("TYPESAFE_API_KEY")
            ).strip()
        else:
            api_key = ""

        context = args.get("state")
        decision = decide(
            backend=backend,
            question=question,
            options=options,
            state=_bound_state(context),
            model=self.settings.decide_model,
            api_key=api_key,
            min_confidence=self.settings.decide_min_confidence,
            chat=self.coach,
        )
        rationale = str(decision.rationale or "")[:_RATIONALE_MAX]
        committed: list[Any] = []

        def commit(state: Any) -> Any:
            committed.append(state)
            _charge(state, decision.cost_usd)
            self._leave_budget(session_id)
            if decision.gate == "error":
                # The attempt was made and billed nowhere: count it, or the
                # budget under-counts by every failed decide call.
                state.calls["decide_unpriced"] = (
                    int(state.calls.get("decide_unpriced") or 0) + 1
                )
                state.last_error = rationale
                return _error(rationale, spend_usd=state.spend_usd)
            state.last_error = ""
            state.last_decision = {
                "question": question,
                "decision": decision.decision,
                "confidence": decision.confidence,
                "gate": decision.gate,
                "rationale": rationale,
                "backend": backend,
                "at": _now_iso(),
            }

        error = self._save_with_retry(session_id, commit, what="decide charge")
        if error is not None:
            return error
        state = committed[0] if committed else self.store.load(session_id)
        self._log_decision(
            session_id, turn_id, decision, backend, question=question, options=options
        )
        self.card_text(
            _decision_card(decision, backend)
        )
        return {
            "ok": True,
            "decision": decision.decision,
            "confidence": decision.confidence,
            "probabilities": decision.probabilities,
            "gate": decision.gate,
            "rationale": rationale,
            "cost_usd": decision.cost_usd,
            "spend_usd": state.spend_usd,
            "backend": backend,
            "calls_this_turn": TurnCounters(state.calls).get("decide", turn_id),
            "error": "",
        }

    def _surface_kind(self) -> str:
        value: Any = self.surface
        if callable(value):
            try:
                value = value()
            except Exception:
                return "cli"
        if not isinstance(value, str):
            value = getattr(value, "value", value)
        text = str(value or "").strip().lower()
        return text or "cli"

    def _is_child(self, session_id: str) -> bool:
        return (
            session_id in self._child_sessions
            or self._surface_kind() in _CHILD_SURFACES
        )


def _with_host_ids(
    args: Mapping[str, Any], kw: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge the dispatcher's session/turn ids into the tool arguments.

    The dispatcher always wins, and when it supplies nothing there is no
    authoritative identity: the tool must not let model-controlled JSON name a
    session, because that would read another chat's state, reset the per-turn
    call caps, and park spend in a bucket the model chooses. Such calls share
    the ``current`` bucket instead.
    """
    merged = dict(args)
    session = kw.get("session_id")
    merged["session_id"] = str(session) if session else "current"
    task = kw.get("turn_id") or kw.get("task_id")
    merged["turn_id"] = str(task) if task else "current"
    return merged

def _default_root() -> Path:
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return home / "coach"


def _primary_identity() -> tuple[str, str]:
    """Provider and model the main agent runs, for the same-model guard."""
    try:
        from hermes_cli.config import load_config_readonly

        section = (load_config_readonly() or {}).get("model")
        if isinstance(section, Mapping):
            model = str(section.get("default") or "")
            provider = str(section.get("provider") or "")
            if not provider:
                head, tail, _ = split_provider_model(model)
                if head:
                    provider, model = head, tail
            return model, provider
    except Exception:
        pass
    return "", ""


def build_plugin(ctx: Any) -> CoachPlugin:
    """Build the plugin from the host context and persisted config."""

    def read_settings() -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, default in DEFAULTS.items():
            try:
                values[key] = ctx.get_config(key, default)
            except Exception:
                values[key] = default
        return values

    raw = read_settings()
    root = _default_root()
    primary_model, primary_provider = _primary_identity()
    plugin = CoachPlugin(
        llm=ctx.llm,
        store=Store(root / "sessions"),
        settings=load_settings(raw),
        primary_model=str(
            getattr(ctx, "model", "") or getattr(ctx, "model_name", "") or primary_model
        ),
        primary_provider=str(
            getattr(ctx, "provider", "") or getattr(ctx, "model_provider", "") or primary_provider
        ),
        post=getattr(ctx, "post_message", None),
        inject=getattr(ctx, "inject_message", None),
        surface=getattr(ctx, "session_surface", lambda: "cli"),
        cwd=getattr(ctx, "session_cwd", lambda: os.getcwd()),
        clock=time.time,
        guidance=Guidance(root),
        set_setting=getattr(ctx, "set_config", None),
        settings_reader=read_settings,
    )
    plugin.worker = Worker(
        plugin.run_review, timeout=lambda: plugin.settings.worker_timeout_s
    )
    return plugin

