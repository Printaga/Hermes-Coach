# tests/test_jev.py
import json
from types import SimpleNamespace

from coach.jev import decide


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_openrouter_choice_is_parsed():
    body = {
        "answers": {"path": {"type": "choice", "choice": "b"}},
        "verdicts": {"path": {"gate": "act", "confidence": 0.82}},
        "usage": {"cost": 0.0004},
    }
    decision = decide(
        backend="jev-openrouter",
        question="Which?",
        options=["a", "b"],
        state={"goal": "x"},
        api_key="k",
        http=lambda req, **kw: Resp(body),
    )
    assert decision.decision == "b"
    assert decision.gate == "act"
    assert decision.confidence == 0.82
    assert decision.cost_usd == 0.0004


def test_low_confidence_escalates():
    body = {
        "answers": {"path": {"choice": "a"}},
        "verdicts": {"path": {"gate": "act", "confidence": 0.31}},
        "usage": {},
    }
    decision = decide(
        backend="jev-openrouter",
        question="q",
        options=["a", "b"],
        state={},
        api_key="k",
        min_confidence=0.6,
        http=lambda req, **kw: Resp(body),
    )
    assert decision.gate == "escalate"


def test_missing_key_is_an_error_not_a_call():
    decision = decide(
        backend="jev-openrouter",
        question="q",
        options=["a"],
        state={},
        api_key="",
    )
    assert decision.gate == "error" and decision.rationale


def test_choice_outside_options_is_an_error():
    body = {
        "answers": {"path": {"choice": "c"}},
        "verdicts": {"path": {"gate": "act", "confidence": 0.9}},
        "usage": {},
    }
    decision = decide(
        backend="jev-openrouter",
        question="q",
        options=["a", "b"],
        state={},
        api_key="k",
        http=lambda req, **kw: Resp(body),
    )
    assert decision.gate == "error"


def test_http_failure_is_an_error():
    def boom(req, **kw):
        raise OSError("endpoint down")

    decision = decide(
        backend="jev-openrouter",
        question="q",
        options=["a", "b"],
        state={},
        api_key="k",
        http=boom,
    )
    assert decision.gate == "error" and "down" in decision.rationale


def test_chat_model_maps_a_structured_choice():
    class Chat:
        def __init__(self):
            self.kw = {}

        def ask(self, messages, **kw):
            self.kw = kw
            return SimpleNamespace(
                parsed={"choice": "a", "confidence": 0.9, "rationale": "cheaper"},
                cost_usd=0.002,
                error="",
            )

    chat = Chat()
    decision = decide(
        backend="chat-model",
        question="Which?",
        options=["a", "b"],
        state={"goal": "x"},
        chat=chat,
    )
    assert decision.decision == "a" and decision.gate == "act"
    assert decision.cost_usd == 0.002
    assert chat.kw["structured"] is True
    assert chat.kw["schema"]["properties"]["choice"]["enum"] == ["a", "b"]
