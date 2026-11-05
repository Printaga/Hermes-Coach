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
