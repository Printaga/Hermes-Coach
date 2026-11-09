"""TurnCounters is the one reserve/release/prune implementation."""

from coach.counters import TurnCounters


def test_reserve_and_release_are_floored_at_zero():
    calls = {}
    counters = TurnCounters(calls)
    assert counters.reserve("consult", "t1") == 1
    assert counters.reserve("consult", "t1") == 2
    assert counters.release("consult", "t1") == 1
    assert counters.release("consult", "t1") == 0
    assert counters.release("consult", "t1") == 0
    assert calls["consult:t1"] == 0


def test_namespaces_are_independent():
    counters = TurnCounters({})
    counters.reserve("consult", "t1")
    assert counters.get("decide", "t1") == 0
    assert counters.get("consult", "t2") == 0


def test_prune_keeps_live_turns_and_global_telemetry():
    calls = {
        "review_skips": 3,
        "decide_unpriced": 1,
        "consult:t1": 2,
        "consult:old": 5,
        "review_holds:t1": 1,
    }
    TurnCounters(calls).prune({"t1"})
    assert calls == {
        "review_skips": 3,
        "decide_unpriced": 1,
        "consult:t1": 2,
        "review_holds:t1": 1,
    }


def test_a_corrupt_counter_reads_as_zero():
    assert TurnCounters({"consult:t1": "nope"}).get("consult", "t1") == 0
