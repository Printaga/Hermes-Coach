# tests/test_command.py
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.test_plugin_ask import Llm


def _p(tmp_path):
    return CoachPlugin(
        llm=Llm(),
        store=Store(tmp_path),
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
    )


def test_status_lists_modes_and_spend(tmp_path):
    out = _p(tmp_path).command("status")
    assert "Active" in out and "Review" in out and "$" in out


def test_unconfigured_status_explains(tmp_path):
    p = CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings({}))
    assert "Unconfigured" in p.command("status")


def test_set_toggles_persist_through_callback(tmp_path):
    saved = {}
    p = CoachPlugin(
        llm=Llm(),
        store=Store(tmp_path),
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
        set_setting=lambda k, v: saved.__setitem__(k, v),
    )
    assert "off" in p.command("active off").lower()
    assert saved["active_enabled"] is False


def test_unknown_subcommand_prints_help(tmp_path):
    assert "/coach" in _p(tmp_path).command("wat")
