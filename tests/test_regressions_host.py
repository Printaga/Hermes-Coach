# Cross-cutting regression tests for issues found while building the plugin.
#
# Each test pins one behavior that broke, misled, or silently degraded the
# plugin during development or live verification. Unit tests next to each
# module cover the happy paths; these tests cover the failure modes.

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from coach.advisor import Coach
from coach.plugin import (
    ASK_DESCRIPTION,
    ASK_SCHEMA,
    DECIDE_DESCRIPTION,
    DECIDE_SCHEMA,
    CoachPlugin,
    _primary_identity,
    build_plugin,
)
from coach.settings import DEFAULTS, load_settings
from coach.state import Store
from tests.support import AdvisorLlm as _StructuredLlmForAdvisor
from tests.support import ConsultLlm as _ConsultLlm
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
# Packaging and host contract
# ---------------------------------------------------------------------------


def test_plugin_package_uses_relative_imports_only():
    """The host mounts this package synthetically, so absolute imports fail."""
    bad = []
    candidates = [REPO / "__init__.py", *sorted((REPO / "coach").glob("*.py"))]
    for path in candidates:
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            stripped = line.strip()
            if (
                stripped.startswith("from coach.")
                or stripped.startswith("from coach import")
                or stripped.startswith("import coach.")
            ):
                bad.append(f"{path.name}:{number}:{stripped}")
    assert not bad, bad


def test_root_register_imports_the_package_lazily():
    """Importing the loader entry must not import the package at module scope."""
    text = _product_text("__init__.py")
    head, _, body = text.partition("def register")
    assert "from .coach.plugin import" not in head
    assert "from .coach.plugin import" in body


def test_dashboard_module_loads_from_its_own_file_without_repo_on_path(tmp_path):
    """The host loads plugin_api.py by path, so it must fix sys.path itself."""
    module_path = (REPO / "dashboard" / "plugin_api.py").as_posix()
    script = (
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('coach_dashboard_api', r'''{module_path}''')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "paths = {getattr(route, 'path', None) for route in module.router.routes}\n"
        "required = {'/status', '/config', '/guidance', '/recent', '/permissions/grant'}\n"
        "assert required <= paths, paths\n"
        "assert callable(module.app_for_tests)\n"
        "print('dashboard file load ok')\n"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_register_wires_tools_hooks_command_and_shutdown():
    text = _product_text("__init__.py")
    for token in (
        "coach_ask",
        "coach_decide",
        "pre_verify",
        "post_tool_call",
        "post_llm_call",
        "pre_llm_call",
        "subagent_start",
        "subagent_stop",
        "on_session_end",
        "register_command",
        "on_unload(plugin.shutdown)",
    ):
        assert token in text, token


def test_manifest_contract_matches_the_live_plugin():
    manifest = yaml.safe_load((REPO / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "coach"
    assert manifest["kind"] == "standalone"
    # No config_schema: a schema would make the host render a second,
    # duplicate "Agent settings" tab beside the Coach page.
    assert not manifest.get("config_schema")
    assert set(manifest["provides_tools"]) == {"coach_ask", "coach_decide"}
    assert set(manifest["provides_hooks"]) == {
        "pre_verify",
        "post_tool_call",
        "post_llm_call",
        "pre_llm_call",
        "subagent_start",
        "subagent_stop",
        "on_session_end",
    }


def test_tool_schemas_and_descriptions_are_host_ready():
    assert ASK_SCHEMA["required"] == ["question"]
    assert ASK_SCHEMA["additionalProperties"] is False
    assert ASK_SCHEMA["properties"]["question"]["maxLength"] == 4000
    assert DECIDE_SCHEMA["required"] == ["question", "options"]
    assert DECIDE_SCHEMA["additionalProperties"] is False
    assert "Relay" in ASK_DESCRIPTION and "attributed" in ASK_DESCRIPTION
    assert "act" in DECIDE_DESCRIPTION and "escalate" in DECIDE_DESCRIPTION


def test_test_script_runs_both_gates():
    text = _product_text("scripts/test.sh")
    assert "pytest" in text
    assert "${@:-tests}" in text
    assert "check-desktop-page.mjs" in text
    assert "pipefail" in text


# ---------------------------------------------------------------------------
# Configuration and settings validation
# ---------------------------------------------------------------------------


def test_guidance_lives_in_files_not_settings_keys():
    assert [key for key in DEFAULTS if key.startswith("guidance_")] == [
        "guidance_enabled"
    ]


def test_settings_reject_non_finite_and_negative_numbers():
    settings = load_settings(
        {
            "budget_session_usd": float("nan"),
            "ledger_max_tokens": float("inf"),
            "ledger_max_tool_result_chars": -10,
            "decide_min_confidence": 2,
        }
    )
    assert settings.budget_session_usd == DEFAULTS["budget_session_usd"]
    assert settings.ledger_max_tokens >= 500
    assert settings.ledger_max_tool_result_chars >= 50
    assert settings.decide_min_confidence == 1.0


def test_model_ids_with_slashes_survive_settings():
    settings = load_settings(
        {"coach_model": {"provider": "openrouter", "model": "a/b/c"}}
    )
    assert settings.coach_model.model == "a/b/c"
    assert settings.model_for("consult").model == "a/b/c"


def test_unknown_tool_mode_has_no_model():
    settings = load_settings({"coach_model": {"provider": "p", "model": "m"}})
    assert settings.model_for("bogus") is None


# ---------------------------------------------------------------------------
# Models, timeouts, budgets, and host identity
# ---------------------------------------------------------------------------


def test_structured_calls_carry_system_prompt_instructions_and_schema():
    llm = _StructuredLlmForAdvisor()
    coach = Coach(llm, load_settings({"coach_model": {"provider": "p", "model": "m"}}))
    coach.ask(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "body"}],
        mode="consult",
        structured=True,
        schema={"type": "object"},
        instructions="Do X",
        timeout=9,
    )
    call = llm.calls[0]
    assert call["system_prompt"] == "sys"
    assert call["instructions"] == "Do X"
    assert call["json_schema"] == {"type": "object"}
    assert call["timeout"] == 9


def test_consult_uses_the_configured_worker_timeout(tmp_path):
    plugin = _plugin(
        tmp_path,
        worker_timeout_s=7,
    )
    plugin.tool_ask({"question": "q"})
    assert plugin.llm.calls[0]["timeout"] == 7.0


def test_warn_budget_does_not_stop_automatic_modes(tmp_path):
    plugin = _plugin(
        tmp_path,
        budget_session_usd=0.01,
        budget_on_exceed="warn",
        active_enabled=True,
    )
    state = plugin.store.load("s")
    state.spend_usd = 1.0
    plugin.store.save(state)
    assert (
        plugin.should_review(
            state,
            {"turn_id": "t1", "tools": [{"name": "read_file"}]},
            plugin.settings,
        )
        is True
    )
    assert json.loads(plugin.tool_ask({"question": "q"}, session_id="s"))["ok"] is True


def test_hard_budget_stops_decide_tool_calls(tmp_path, monkeypatch):
    from tests.support import decide_plugin, decide_result

    plugin = decide_plugin(tmp_path, budget_session_usd=0.01, budget_on_exceed="hard")
    state = plugin.store.load("current")
    state.spend_usd = 1.0
    plugin.store.save(state)
    monkeypatch.setattr(
        "coach.plugin.decide", lambda **kwargs: decide_result()
    )
    assert (
        json.loads(
            plugin.tool_decide({"question": "q", "options": ["a", "b"]})
        )["error"]
        == "budget_exhausted"
    )


def test_decide_merges_dispatcher_turn_and_session_ids(tmp_path, monkeypatch):
    from coach.jev import Decision
    from tests.support import decide_plugin

    plugin = decide_plugin(tmp_path)
    monkeypatch.setattr(
        "coach.plugin.decide",
        lambda **kwargs: Decision("a", 0.8, {"a": 0.8}, "act", "ok", 0.0),
    )
    out = json.loads(
        plugin.tool_decide(
            {"question": "q", "options": ["a", "b"]},
            session_id="s1",
            task_id="task-9",
        )
    )
    assert out["ok"] is True
    assert plugin.store.load("s1").calls["decide:task-9"] == 1


def test_decide_rejects_duplicate_or_blank_options(tmp_path, monkeypatch):
    from tests.support import decide_plugin, decide_result

    plugin = decide_plugin(tmp_path)
    monkeypatch.setattr(
        "coach.plugin.decide", lambda **kwargs: decide_result()
    )
    assert (
        json.loads(
            plugin.tool_decide({"question": "q", "options": ["a", " a "]})
        )["error"]
        == "options_invalid"
    )


def test_jev_request_envelope_carries_model_state_and_auth():
    from coach.jev import decide

    seen = {}

    class Resp:
        def read(self):
            return json.dumps(
                {
                    "answers": {"choice": {"type": "choice", "choice": "a"}},
                    "verdicts": {
                        "choice": {"gate": "act", "confidence": 0.8}
                    },
                    "usage": {},
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_http(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return Resp()

    decision = decide(
        backend="jev-openrouter",
        question="q",
        options=["a", "b"],
        state={"goal": "x"},
        api_key="k",
        http=fake_http,
    )
    assert decision.decision == "a"
    assert seen["url"].endswith("/api/alpha/decisions")
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer k"
    assert seen["timeout"] == 15
    assert seen["body"]["model"] == "typesafe/jev-1.14"
    assert seen["body"]["state"] == {"goal": "x"}
    assert set(seen["body"]["questions"]["choice"]["criteria"]) == {"a", "b"}


def test_jev_treats_non_numeric_confidence_as_no_evidence():
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

    body = {
        "answers": {"choice": {"choice": "a"}},
        "verdicts": {"choice": {"gate": "act", "confidence": "high"}},
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
    assert decision.gate == "escalate"



def test_primary_identity_prefers_the_live_hook_model(monkeypatch):
    import sys
    import types

    parent = types.ModuleType("hermes_cli")
    parent.__path__ = []
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config_readonly = lambda: {
        "model": {"default": "openrouter/m"}
    }
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setattr(parent, "config", config_module, raising=False)
    assert _primary_identity() == ("m", "openrouter")


def test_build_plugin_reads_the_session_working_directory(tmp_path):
    llm = _ConsultLlm()
    ctx = SimpleNamespace(
        get_config=lambda key, default: default,
        llm=llm,
        model="",
        model_name="",
        provider="",
        model_provider="",
        post_message=None,
        inject_message=None,
        session_surface=lambda: "desktop",
        session_cwd=lambda: "/proj",
        set_config=None,
    )
    plugin = build_plugin(ctx)
    try:
        assert plugin.cwd() == "/proj"
    finally:
        plugin.shutdown()


def test_guidance_read_refuses_a_symlinked_COACH_md(tmp_path):
    from coach.guidance import Guidance

    secret = tmp_path / "outside.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "COACH.md").symlink_to(secret)
    guidance = Guidance(tmp_path / "coach")
    assert guidance.read("project", str(project)) == ""
    assert "TOP-SECRET" not in guidance.system_block(str(project))
