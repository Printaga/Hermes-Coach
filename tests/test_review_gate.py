# tests/test_review_gate.py
from coach.findings import Finding
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.support import ConsultLlm as Llm
from tests.support import StructuredLlm


def _p(tmp_path, *, llm=None, **cfg):
    base = {"coach_model": {"provider": "p", "model": "m"}}
    base.update(cfg)
    posted = []
    p = CoachPlugin(
        llm=llm or Llm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings(base),
        post=posted.append,
    )
    p._test_post = posted
    return p


PAY = dict(
    session_id="s",
    turn_id="t1",
    final_response="All done.",
    changed_paths=["a.py"],
    model="primary",
    coding=True,
    attempt=0,
)

FINDING = {
    "severity": "blocker",
    "title": "Deletes before copying",
    "body": "The copy happens after the delete.",
    "evidence": "store.py:40",
    "fix": "Copy first.",
}


def test_review_skipped_when_disabled(tmp_path):
    p = _p(tmp_path, review_enabled=False)
    assert p.on_pre_verify(**PAY) is None


def test_report_authority_never_holds(tmp_path):
    p = _p(tmp_path, review_authority="report", llm=StructuredLlm(FINDING))
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/a.py"
    out = p.on_pre_verify(**PAY)
    assert out is None or "action" not in out
    assert p._test_post


def test_hold_is_capped_per_request(tmp_path):
    p = _p(tmp_path, review_authority="hold_once")
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/a.py"
    p.review_now = lambda **kw: type(
        "F",
        (),
        {
            "severity": "blocker",
            "title": "t",
            "body": "b",
            "evidence": "e",
            "fix": "f",
        },
    )()
    first = p.on_pre_verify(**PAY)
    second = p.on_pre_verify(**PAY)
    assert first["action"] == "continue"
    assert second is None


def test_no_changed_paths_skips_the_call(tmp_path):
    p = _p(tmp_path)

    def fail(**kw):
        raise AssertionError("review_now should not run")

    p.review_now = fail
    assert p.on_pre_verify(**dict(PAY, changed_paths=[])) is None


def test_budget_soft_stop_skips_review(tmp_path):
    p = _p(tmp_path, budget_session_usd=0.01, budget_on_exceed="soft")
    state = p.store.load("s")
    state.spend_usd = 1.0
    p.store.save(state)
    called = []
    p.review_now = lambda **kw: called.append(kw) or None
    assert p.on_pre_verify(**PAY) is None
    assert called == []


def test_review_now_uses_a_bounded_deadline(tmp_path):
    llm = StructuredLlm(FINDING)
    p = _p(tmp_path, llm=llm)
    finding = p.review_now(
        session_id="s",
        turn_id="t1",
        request="Fix the store.",
        claim="All done.",
        evidence="diff --git a/store.py",
    )
    assert finding is not None and finding.severity == "blocker"
    assert llm.calls and llm.calls[0]["timeout"] == 16.0


def test_non_blocker_is_reported_as_a_card(tmp_path):
    p = _p(tmp_path)
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/a.py"
    p.review_now = lambda **kw: Finding(
        "concern", "Missing check", "No validation.", "a.py:1", "Validate."
    )
    assert p.on_pre_verify(**PAY) is None
    assert p._test_post and "Missing check" in p._test_post[0]


def test_evidence_uses_the_session_cwd(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    p = CoachPlugin(
        llm=Llm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
        cwd=lambda: str(project),
    )
    seen = {}
    p.collect_evidence = lambda cwd, paths, base="": seen.setdefault("cwd", cwd) or ""
    p.review_now = lambda **kw: None
    p.on_pre_verify(**PAY)
    assert seen["cwd"] == str(project)


def test_review_now_logs_one_line(tmp_path):
    p = _p(tmp_path, llm=StructuredLlm(FINDING))
    p.review_now(session_id="s", turn_id="t1", request="r", claim="c", evidence="e")
    from coach.plugin import _log_name

    lines = (tmp_path / "logs" / f"{_log_name('s')}.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert '"severity": "blocker"' in lines[0]
    assert '"body": "The copy happens after the delete."' in lines[0]


def test_review_uses_the_last_user_message_as_request(tmp_path):
    p = _p(tmp_path)
    p.on_post_llm_call(
        session_id="s", turn_id="t0", user_message="Add a retry cap",
        assistant_response="ok", conversation_history=[], model="m",
    )
    seen = {}
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/a.py"
    p.review_now = lambda **kw: seen.update(kw) or None
    p.on_pre_verify(session_id="s", turn_id="t1", final_response="done",
                    changed_paths=["a.py"], cwd=str(tmp_path))
    assert seen["request"] == "Add a retry cap"


def test_the_gate_fits_inside_the_host_hook_timeout(tmp_path, monkeypatch):
    """A 20 s review plus up to 8 s of git overran the host's 30 s hook timeout.

    The host then skipped the callback, so one slow review silenced the gate.
    """
    import coach.plugin as plugin_mod

    clock = iter([100.0, 100.0, 101.0])
    monkeypatch.setattr(plugin_mod.time, "monotonic", lambda: next(clock, 101.0))
    p = _p(tmp_path)
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/a.py"
    seen = {}
    p.review_now = lambda **kw: seen.update(kw) or None
    p.on_pre_verify(**PAY)
    assert 0 < seen["timeout"] <= 16.0


def test_the_gate_skips_the_model_when_evidence_used_the_window(tmp_path, monkeypatch):
    import coach.plugin as plugin_mod

    clock = iter([100.0, 100.0, 121.0, 121.0, 121.0])
    monkeypatch.setattr(plugin_mod.time, "monotonic", lambda: next(clock, 121.0))
    p = _p(tmp_path)
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/a.py"

    def never(**kw):
        raise AssertionError("no time left: the review must not start")

    p.review_now = never
    assert p.on_pre_verify(**PAY) is None


def _concern():
    return {
        "severity": "concern",
        "title": "Retry hides the timeout",
        "body": "The retry loop swallows the deadline error.",
        "evidence": "client.py:88",
        "fix": "Re-raise after the last attempt.",
    }


def test_a_concern_is_queued_for_the_model_not_only_carded(tmp_path):
    """The bug: a review finding was a card the model never read."""
    p = _p(tmp_path, llm=StructuredLlm(_concern()))
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/client.py"

    p.on_pre_verify(
        session_id="s",
        turn_id="t1",
        final_response="done",
        changed_paths=["client.py"],
        cwd=str(tmp_path),
    )

    state = p.store.load("s")
    assert len(state.pending) == 1
    note = state.pending[0]
    assert note["turn_id"] == "t1"
    assert "<coach-note" in note["text"]
    assert "Retry hides the timeout" in note["text"]
    assert any("Coach · concern" in text for text in p._test_post)


def test_the_queued_note_reaches_the_next_turn_as_context(tmp_path):
    p = _p(tmp_path, llm=StructuredLlm(_concern()))
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/client.py"
    p.on_pre_verify(
        session_id="s",
        turn_id="t1",
        final_response="done",
        changed_paths=["client.py"],
        cwd=str(tmp_path),
    )

    context = p.on_pre_llm_call({"session_id": "s", "turn_id": "t2"})
    assert context and "<coach-note" in context["context"]
    assert p.store.load("s").pending == []


def test_the_gate_never_starts_a_turn_through_delivery_policy(tmp_path):
    injected = []
    p = _p(
        tmp_path,
        llm=StructuredLlm({**_concern(), "severity": "blocker"}),
        delivery_policy="corrective",
        review_authority="report",
    )
    p.inject = lambda text: injected.append(text) or True
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/client.py"

    p.on_pre_verify(
        session_id="s",
        turn_id="t1",
        final_response="done",
        changed_paths=["client.py"],
        cwd=str(tmp_path),
    )
    assert injected == []
    assert len(p.store.load("s").pending) == 1


def test_a_hold_posts_a_card_and_a_complete_message(tmp_path):
    p = _p(tmp_path, llm=StructuredLlm({**_concern(), "severity": "blocker"}))
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/client.py"

    out = p.on_pre_verify(
        session_id="s",
        turn_id="t1",
        final_response="done",
        changed_paths=["client.py"],
        cwd=str(tmp_path),
    )

    assert out["action"] == "continue"
    message = out["message"]
    assert "Retry hides the timeout" in message
    assert "swallows the deadline error" in message
    assert "client.py:88" in message
    assert "Re-raise" in message
    assert any("Coach · blocker" in text for text in p._test_post)


def test_the_same_finding_is_queued_once_per_turn(tmp_path):
    p = _p(tmp_path, llm=StructuredLlm(_concern()))
    finding = Finding(**{
        "severity": "concern",
        "title": "Retry hides the timeout",
        "body": "b",
        "evidence": "",
        "fix": "",
    })
    p.deliver(finding, session_id="s", turn_id="t1")
    p.deliver(finding, session_id="s", turn_id="t1")
    assert len(p.store.load("s").pending) == 1


def test_cards_can_be_switched_off_entirely(tmp_path):
    p = _p(tmp_path, llm=StructuredLlm(_concern()), delivery_show_cards=False)
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/client.py"

    p.on_pre_verify(
        session_id="s",
        turn_id="t1",
        final_response="done",
        changed_paths=["client.py"],
        cwd=str(tmp_path),
    )
    assert p._test_post == []
    assert len(p.store.load("s").pending) == 1


def test_a_second_claim_is_reviewed_after_a_hold(tmp_path):
    """The claim that answers a held blocker must still be checked."""
    llm = StructuredLlm({**_concern(), "severity": "blocker"})
    p = _p(tmp_path, llm=llm)
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/client.py"

    first = p.on_pre_verify(**PAY)
    second = p.on_pre_verify(**PAY)

    assert first and first["action"] == "continue"
    assert len(llm.calls) == 2, "the answering claim was not reviewed"
    assert second is None  # the hold cap still permits only one hold


def test_hold_cap_is_rechecked_after_the_review(tmp_path):
    """A concurrent gate firing may use the last hold mid-review; the commit
    must re-check the cap instead of letting both firings hold."""
    p = _p(tmp_path, review_authority="hold_once", review_max_holds=1)
    p.collect_evidence = lambda cwd, paths, base="": "diff --git a/a.py"

    def concurrent_review(**kw):
        with p._store_lock:
            state = p.store.load("s")
            state.calls["review_holds:t1"] = 1
            state.updated_at = 0.0
            p.store.save(state)
        return type(
            "F",
            (),
            {
                "severity": "blocker",
                "title": "t",
                "body": "b",
                "evidence": "e",
                "fix": "f",
            },
        )()

    p.review_now = concurrent_review
    out = p.on_pre_verify(**PAY)
    assert p.store.load("s").calls.get("review_holds:t1") == 1
    assert out is None or "action" not in out


def test_record_skip_counts_and_names_the_reason(tmp_path):
    """Every swallowed skip must leave a counter + readable last_error."""
    p = _p(tmp_path)
    p._record_skip("s", "review failed: Boom")
    p._record_skip("s", "gate skipped: 0.0s left after evidence")
    state = p.store.load("s")
    assert state.calls.get("review_skips") == 2
    assert "gate skipped" in state.last_error
