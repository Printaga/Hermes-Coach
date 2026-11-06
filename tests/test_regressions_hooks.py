# Cross-cutting regression tests for issues found while building the plugin.
#
# Each test pins one behavior that broke, misled, or silently degraded the
# plugin during development or live verification. Unit tests next to each
# module cover the happy paths; these tests cover the failure modes.

import copy
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from coach.advisor import Coach, _content_text, _differs, _split_messages
from coach.delivery import format_ambient, format_card, format_corrective, route
from coach.commands import _bool_arg, _coerce_setting, _parse_model
from coach.evidence import collect
from coach.findings import Finding, is_deliverable, parse_finding
from coach.jev import Decision, _inner, _probabilities, decide
from coach.ledger import append_turn, build_messages, tool_card
from coach.plugin import (
    ASK_DESCRIPTION,
    ASK_SCHEMA,
    DECIDE_DESCRIPTION,
    DECIDE_SCHEMA,
    CoachPlugin,
    _as_turn,
    _child_id,
    _find_turn,
    _last_reviewed_digest,
    _payload,
    _primary_identity,
    _record_turn,
    _request_text,
    _result_text,
    _tool_arg,
    _turns_since_review,
    build_plugin,
)
from coach.settings import DEFAULTS, load_settings
from coach.state import Store
from dashboard.plugin_api import app_for_tests
from tests.support import AdvisorLlm as _StructuredLlmForAdvisor
from tests.support import ConsultLlm as _ConsultLlm
from tests.support import FakeWorker as _FakeWorker
from tests.support import StructuredLlm as _StructuredLlm
from tests.support import product_text as _product_text

REPO = Path(__file__).resolve().parent.parent


def _plugin(tmp_path, **config):
    base = {"coach_model": {"provider": "openrouter", "model": "x"}}
    base.update(config)
    return CoachPlugin(
        llm=_ConsultLlm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings(base),
    )


# ---------------------------------------------------------------------------
# Hooks, commands, and registration
# ---------------------------------------------------------------------------


def test_post_tool_calls_are_ignored_when_active_is_off(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=False)
    plugin.on_post_tool_call(
        session_id="s",
        turn_id="t",
        tool_name="read_file",
        args={"path": "a.py"},
        result="ok",
    )
    assert plugin._turn_tools == {}


def test_post_llm_call_records_intent_for_review_without_active(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=False, review_enabled=True)
    plugin.on_post_llm_call(
        session_id="s",
        turn_id="t1",
        user_message="Add a retry cap",
        assistant_response="ok",
        conversation_history=[],
        model="m",
    )
    assert plugin.store.load("s").last_user_message == "Add a retry cap"


def test_queued_active_review_is_replaced_by_a_newer_turn(tmp_path):
    plugin = _plugin(tmp_path, active_enabled=True)
    plugin.worker = _FakeWorker()
    plugin.on_post_tool_call(
        session_id="s",
        turn_id="t1",
        tool_name="read_file",
        args={"path": "a.py"},
        result="ok",
    )
    plugin.on_post_llm_call(
        session_id="s",
        turn_id="t1",
        user_message="u",
        assistant_response="a",
        conversation_history=[],
        model="m",
    )
    plugin.on_post_tool_call(
        session_id="s",
        turn_id="t2",
        tool_name="read_file",
        args={"path": "b.py"},
        result="ok",
    )
    plugin.on_post_llm_call(
        session_id="s",
        turn_id="t2",
        user_message="u",
        assistant_response="a",
        conversation_history=[],
        model="m",
    )
    assert plugin.worker.submitted == [("s", "t1"), ("s", "t2")]
    assert set(plugin._queued) == {("s", "t2")}


def test_unconfigured_plugin_never_queues_active_reviews(tmp_path):
    plugin = CoachPlugin(
        llm=_ConsultLlm(),
        store=Store(tmp_path),
        settings=load_settings({"active_enabled": True}),
    )
    state = plugin.store.load("s")
    assert (
        plugin.should_review(
            state,
            {"turn_id": "t1", "tools": [{"name": "read_file"}]},
            plugin.settings,
        )
        is False
    )


def test_command_rejects_bad_modes_models_and_settings(tmp_path):
    plugin = _plugin(tmp_path)
    assert "Usage" in plugin.command("model bogus p/m")
    assert "Use provider/model" in plugin.command("model consult nope")
    assert "Usage" in plugin.command("active")
    assert "Unknown setting" in plugin.command("set nope 1")
    assert "Invalid value" in plugin.command("set active_every_n nope")


def test_command_can_clear_a_per_mode_model(tmp_path):
    plugin = _plugin(
        tmp_path, review_model={"provider": "p", "model": "m"}
    )
    out = plugin.command("model review clear")
    assert "none set" in out.lower()
    assert plugin.settings.review_model is None


def test_model_command_rejects_unparseable_values(tmp_path):
    assert _parse_model("openrouter/")[0] is False
    assert _parse_model("/model")[0] is False
    assert _parse_model("just-a-name")[0] is False
    assert _coerce_setting("active_enabled", "maybe") == (False, None)
