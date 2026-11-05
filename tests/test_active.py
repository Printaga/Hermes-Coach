# tests/test_active.py
import json
import threading

from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from coach.worker import Worker
from tests.test_plugin_ask import Llm


class FakeWorker:
    def __init__(self):
        self.submitted = []

    def submit(self, session_id, turn_id=""):
        self.submitted.append(session_id)
        return True


class StructuredLlm:
    """A client that returns one structured finding and counts calls."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        payload = self.payload

        class R:
            text = ""
            usage = None
            parsed = None

        result = R()
        result.parsed = payload
        result.model = kw["model"]
        result.provider = kw["provider"]
        return result


def _p(tmp_path, *, llm=None, **cfg):
    base = {
        "coach_model": {"provider": "p", "model": "m"},
        "active_enabled": True,
    }
    base.update(cfg)
    p = CoachPlugin(
        llm=llm or Llm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings(base),
        cost_fn=lambda model, usage, provider: 0.25,
    )
    p.worker = FakeWorker()
    return p


TURN = dict(
    session_id="s",
    turn_id="t1",
    user_message="fix the bug",
    assistant_response="done",
    conversation_history=[],
    model="primary",
)

FINDING = {
    "severity": "concern",
    "title": "Unchecked write",
    "body": "Writes before validating.",
    "evidence": "a.py:1",
    "fix": "Validate first.",
}


def _tool(p, turn_id="t1", name="read_file", result="ok"):
    p.on_post_tool_call(
        session_id="s",
        turn_id=turn_id,
        tool_name=name,
        args={"path": "a.py"},
        result=result,
    )


def test_chat_only_turn_is_skipped(tmp_path):
    p = _p(tmp_path)
    p.on_post_llm_call(**TURN)
    assert p.worker.submitted == []
    assert p.should_review(p.store.load("s"), {"tools": []}, p.settings) is False


def test_tool_turn_is_queued_once(tmp_path):
    p = _p(tmp_path)
    _tool(p)
    p.on_post_llm_call(**TURN)
    p.on_post_llm_call(**TURN)
    assert p.worker.submitted == ["s"]


def test_disabled_mode_never_queues(tmp_path):
    p = _p(tmp_path, active_enabled=False)
    _tool(p, name="x", result="")
    p.on_post_llm_call(**TURN)
    assert p.worker.submitted == []


def test_budget_soft_stop_blocks_active(tmp_path):
    p = _p(tmp_path, budget_session_usd=0.01, budget_on_exceed="soft")
    st = p.store.load("s")
    st.spend_usd = 1.0
    p.store.save(st)
    _tool(p, name="x", result="")
    p.on_post_llm_call(**TURN)
    assert p.worker.submitted == []


def test_every_n_cadence_reviews_every_nth_turn(tmp_path):
    p = _p(tmp_path, active_cadence="every_n", active_every_n=2)
    p.on_post_llm_call(**dict(TURN, turn_id="t1"))
    assert p.worker.submitted == []
    p.on_post_llm_call(**dict(TURN, turn_id="t2"))
    assert p.worker.submitted == ["s"]


def test_same_digest_is_not_reviewed_twice(tmp_path):
    p = _p(tmp_path)
    _tool(p)
    p.on_post_llm_call(**TURN)
    state = p.store.load("s")
    state.ledger[-1]["reviewed"] = True
    p.store.save(state)

    p.worker = FakeWorker()
    _tool(p)
    p.on_post_llm_call(**TURN)
    assert p.worker.submitted == []


def test_run_review_records_spend_logs_and_delivers(tmp_path):
    p = _p(tmp_path, llm=StructuredLlm(FINDING))
    _tool(p, name="write_file")
    p.on_post_llm_call(**TURN)
    p.run_review("s", "t1")

    state = p.store.load("s")
    assert state.ledger[-1]["reviewed"] is True
    assert state.spend_usd == 0.25
    assert state.last_error == ""
    assert state.pending and state.pending[0]["title"] == "Unchecked write"

    log = tmp_path / "logs" / "s.jsonl"
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["severity"] == "concern" and record["turn_id"] == "t1"


def test_run_review_skips_when_soft_budget_is_exhausted(tmp_path):
    llm = StructuredLlm(FINDING)
    p = _p(
        tmp_path,
        llm=llm,
        budget_session_usd=0.01,
        budget_on_exceed="soft",
    )
    state = p.store.load("s")
    state.spend_usd = 1.0
    state.ledger = [
        {"turn_id": "t1", "user": "u", "assistant": "a", "tools": []}
    ]
    p.store.save(state)

    p.run_review("s", "t1")
    assert llm.calls == []


def test_worker_coalesces_to_the_newest_queued_session():
    started = threading.Event()
    release = threading.Event()
    seen = []

    def run(session_id, turn_id):
        seen.append(session_id)
        if session_id == "first":
            started.set()
            release.wait(2.0)

    worker = Worker(run)
    try:
        assert worker.submit("first") is True
        assert started.wait(2.0) is True
        assert worker.submit("second") is True
        assert worker.submit("third") is True
        release.set()
        assert worker.drain(2.0) is True
        assert seen == ["first", "third"]
    finally:
        release.set()
        worker.stop()


def test_worker_swallows_errors_and_refuses_work_after_stop():
    def run(session_id, turn_id):
        raise RuntimeError("boom")

    worker = Worker(run)
    assert worker.submit("s") is True
    assert worker.drain(2.0) is True
    worker.stop()
    assert worker.submit("s") is False


def test_worker_passes_the_turn_id():
    seen = []
    worker = Worker(lambda session_id, turn_id: seen.append((session_id, turn_id)))
    try:
        assert worker.submit("s", "t1") is True
        assert worker.drain(2.0) is True
        assert seen == [("s", "t1")]
    finally:
        worker.stop()


def test_hold_does_not_clobber_review_spend(tmp_path):
    blocker = {
        "severity": "blocker",
        "title": "Deletes first",
        "body": "Copy happens after the delete.",
        "evidence": "a.py:1",
        "fix": "Copy first.",
    }
    p = _p(tmp_path, llm=StructuredLlm(blocker), review_authority="hold_once")
    p.collect_evidence = lambda cwd, paths: "diff --git a/a.py"
    out = p.on_pre_verify(
        session_id="s", turn_id="t1", final_response="done",
        changed_paths=["a.py"], cwd=str(tmp_path),
    )
    assert out and out["action"] == "continue"
    assert p.store.load("s").spend_usd == 0.25


class SnapshotWorker:
    def __init__(self, store, session_id):
        self.store = store
        self.session_id = session_id
        self.ledger_at_submit = None

    def submit(self, session_id, turn_id=""):
        self.ledger_at_submit = len(self.store.load(self.session_id).ledger)
        return True


def test_turn_is_persisted_before_the_worker_starts(tmp_path):
    p = _p(tmp_path)
    p.worker = SnapshotWorker(p.store, "s")
    _tool(p)
    p.on_post_llm_call(**TURN)
    assert p.worker.ledger_at_submit == 1
