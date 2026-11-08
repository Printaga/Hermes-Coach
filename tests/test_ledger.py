# tests/test_ledger.py
from coach.ledger import append_turn, build_messages, estimate_tokens, tool_card
from coach.state import SessionState


def _state(n):
    s = SessionState(session_id="s")
    for i in range(n):
        append_turn(s, turn_id=f"t{i}", user=f"user message {i}",
                    assistant=f"answer {i}", tools=[])
    return s

def test_every_user_message_survives_the_ceiling():
    s = _state(60)
    s.note = "objective"
    msgs = build_messages(system="sys", guidance="guide", state=s, question=None,
                          max_tokens=600, tool_result_chars=400, recent_turns=2)
    text = "\n".join(m["content"] for m in msgs)
    assert all(f"user message {i}" in text for i in range(60))
    assert len(msgs) >= 2 and msgs[0]["role"] == "system"

def test_old_tool_cards_collapse_first():
    s = _state(1)
    s.ledger[0]["tools"] = [tool_card("read_file", "a.py", "ok", 5000,
                                      "x" * 5000, max_chars=400)] * 8
    msgs = build_messages(system="sys", guidance="", state=s, question=None,
                          max_tokens=300, tool_result_chars=100, recent_turns=1)
    body = msgs[-1]["content"]
    assert "read_file" in body and len(body) < 6000
    assert "x" * 100 in body and "x" * 101 not in body

def test_estimate_is_roughly_quarter_of_chars():
    assert 2 <= estimate_tokens("abcdefgh") <= 4


def test_bound_turn_caps_every_text_field():
    from coach.ledger import bound_turn

    bounded = bound_turn(
        {
            "turn_id": "t",
            "user": "u" * 50_000,
            "assistant": "a" * 50_000,
            "tools": [
                {"name": "x", "arg": "y" * 500, "status": "ok", "preview": "z" * 900}
            ]
            * 40,
        }
    )
    assert len(bounded["user"]) <= 4000
    assert len(bounded["assistant"]) <= 600
    assert len(bounded["tools"]) <= 20
    from coach.ledger import _ARG_CHARS

    assert len(bounded["tools"][0]["arg"]) <= _ARG_CHARS


def test_bound_turn_caps_the_host_scalars_too():
    """turn_id, cwd and digest reach the same write-time bound as the text:
    the docstring promises a bounded record, not only bounded prose."""
    from coach.ledger import bound_turn

    bounded = bound_turn(
        {
            "turn_id": "t" * 500,
            "user": "u",
            "assistant": "a",
            "tools": [],
            "cwd": "/" + "d" * 5000,
            "digest": "g" * 500,
        }
    )
    assert len(bounded["turn_id"]) <= 64
    assert len(bounded["cwd"]) <= 512
    assert len(bounded["digest"]) <= 64


def test_record_turn_bounds_text_and_keeps_earlier_tool_cards():
    from coach.plugin import _record_turn
    from coach.state import SessionState

    state = SessionState(session_id="s")
    card = {"name": "read_file", "arg": "a.py", "status": "ok", "preview": "x"}
    _record_turn(
        state,
        {"turn_id": "t", "user": "u" * 9000, "assistant": "a" * 9000,
         "tools": [card], "cwd": "/repo"},
    )
    # A later model call in the same turn buffered no cards.
    _record_turn(
        state,
        {"turn_id": "t", "user": "u2", "assistant": "done", "tools": [],
         "cwd": "/repo"},
    )
    record = state.ledger[-1]
    assert record["assistant"] == "done"
    assert [c["name"] for c in record["tools"]] == ["read_file"]
    assert record["tools"][0]["arg"] == "a.py"
    assert record["user"] == "u2"  # the later call replaces the text


def test_record_turn_keeps_cards_from_every_model_call():
    from coach.plugin import _record_turn
    from coach.state import SessionState

    state = SessionState(session_id="s")
    first = {"name": "read_file", "arg": "a.py", "status": "ok", "preview": ""}
    second = {"name": "write_file", "arg": "b.py", "status": "ok", "preview": ""}
    _record_turn(state, {"turn_id": "t", "user": "u", "assistant": "a",
                         "tools": [first], "cwd": "/repo"})
    _record_turn(state, {"turn_id": "t", "user": "u", "assistant": "done",
                         "tools": [second], "cwd": "/repo"})
    names = [card["name"] for card in state.ledger[-1]["tools"]]
    assert names == ["read_file", "write_file"]
