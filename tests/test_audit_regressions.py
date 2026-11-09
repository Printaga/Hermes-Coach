"""Regression tests for the findings fixed by the repository audit."""

import json
from types import SimpleNamespace

from coach.findings import Finding
from coach.plugin import _bound_state, _prune_calls
from tests.support import plugin


def test_bound_state_counts_the_escaped_wire_size():
    # jev._post serialises with ensure_ascii=True: 50 CJK values measure far
    # more on the wire than their code-point count.
    data = {f"k{index}": "\u6f22" * 1000 for index in range(50)}
    bounded = _bound_state(data)
    assert len(json.dumps(bounded, ensure_ascii=True)) <= 8000


def test_prune_keeps_the_global_telemetry_counters():
    state = SimpleNamespace(
        ledger=[{"turn_id": "t1"}],
        calls={
            "review_skips": 5,
            "decide_unpriced": 2,
            "consult:t1": 1,
            "consult:old": 1,
        },
    )
    _prune_calls(state)
    assert state.calls == {
        "review_skips": 5,
        "decide_unpriced": 2,
        "consult:t1": 1,
    }


def test_same_title_with_different_advice_keeps_both_notes(tmp_path):
    p = plugin(tmp_path, delivery_policy="ambient")
    first = Finding(
        severity="concern", title="Same", body="first", evidence="e", fix="f"
    )
    second = Finding(
        severity="concern", title="Same", body="second", evidence="e", fix="f"
    )
    p.deliver(first, session_id="s", turn_id="t")
    p.deliver(second, session_id="s", turn_id="t")
    assert len(p.store.load("s").pending) == 2


def test_an_inflight_call_holds_a_floor_against_the_budget(tmp_path):
    p = plugin(
        tmp_path,
        budget_on_exceed="hard",
        budget_session_usd=0.002,
        fallback_price_per_mtok=1.0,
        max_reply_tokens=2000,
    )
    assert p._call_floor() == 0.002
    _state, _tid, first = p._reserve(
        "s", "t1", "consult", budget_modes={"hard"}, limit=10
    )
    assert first is None
    _state, _tid, second = p._reserve(
        "s", "t2", "consult", budget_modes={"hard"}, limit=10
    )
    assert second == {"ok": False, "error": "budget_exhausted"}
    p._leave_budget("s")
    _state, _tid, third = p._reserve(
        "s", "t3", "consult", budget_modes={"hard"}, limit=10
    )
    assert third is None
