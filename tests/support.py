# Shared test-only fakes for the coach plugin suite.
#
# Several older test modules import helpers from each other
# (for example, decide tests reuse the ask test's LLM). New tests should use
# this module instead so helper changes do not couple unrelated test files.

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from coach.jev import Decision
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store


class ConsultLlm:
    """Plain-text coach stand-in that records completion calls."""

    def __init__(self, text="Advice body.", provider="openrouter", model="x"):
        self.text = text
        self.provider = provider
        self.model = model
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append(kwargs)
        self.messages = list(messages)
        usage = SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        return SimpleNamespace(
            text=self.text,
            provider=self.provider,
            model=self.model,
            usage=usage,
            audit={},
        )

    def complete_structured(self, **kwargs):
        raise AssertionError("consult must not use structured output")


class StructuredLlm:
    """Structured coach stand-in that echoes a canned finding."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        result = SimpleNamespace(text="", usage=None, parsed=None)
        result.parsed = self.payload
        result.model = kwargs["model"]
        result.provider = kwargs["provider"]
        return result


class AdvisorLlm:
    """Plain completion refused; one structured payload returned."""

    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {"severity": "none"}

    def complete(self, messages, **kwargs):
        raise AssertionError("structured test must not use plain completion")

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            text="ok",
            provider=kwargs.get("provider", "p"),
            model=kwargs.get("model", "m"),
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
            parsed=self.payload,
            audit={},
        )


class FakeWorker:
    """Records submissions instead of running a thread."""

    def __init__(self):
        self.submitted = []

    def submit(self, session_id, turn_id=""):
        self.submitted.append((session_id, turn_id))
        return True


def product_text(relative_path):
    """Read a file from the plugin checkout, for structure assertions."""
    root = Path(__file__).resolve().parent.parent
    return (root / relative_path).read_text(encoding="utf-8")


class JevResp:
    """Minimal file-like HTTP response for Jev tests."""

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def plugin(tmp_path, **config):
    base = {"coach_model": {"provider": "openrouter", "model": "x"}}
    base.update(config)
    return CoachPlugin(
        llm=ConsultLlm(), store=Store(tmp_path), settings=load_settings(base)
    )


def review_plugin(tmp_path, payload=None, **config):
    base = {"coach_model": {"provider": "openrouter", "model": "m"}}
    base.update(config)
    posted = []
    plugin = CoachPlugin(
        llm=StructuredLlm(
            payload
            or {
                "severity": "blocker",
                "title": "Deletes before copying",
                "body": "The copy happens after the delete.",
                "evidence": "store.py:40",
                "fix": "Copy first.",
            }
        ),
        store=Store(tmp_path / "sessions"),
        settings=load_settings(base),
        post=posted.append,
    )
    plugin._test_post = posted
    return plugin


def decide_plugin(tmp_path, **config):
    base = {
        "coach_model": {"provider": "openrouter", "model": "m"},
        "decide_enabled": True,
    }
    base.update(config)
    return CoachPlugin(
        llm=ConsultLlm(), store=Store(tmp_path), settings=load_settings(base)
    )


def decide_result(**overrides):
    values = {
        "decision": "b",
        "confidence": 0.8,
        "probabilities": {"a": 0.2, "b": 0.8},
        "gate": "act",
        "rationale": "cheaper",
        "cost_usd": 0.0004,
    }
    values.update(overrides)
    return Decision(**values)


PAY = {
    "session_id": "s",
    "turn_id": "t1",
    "final_response": "All done.",
    "changed_paths": ["a.py"],
    "model": "primary",
    "coding": True,
    "attempt": 0,
}
