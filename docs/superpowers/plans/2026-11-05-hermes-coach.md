# Hermes Coach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `coach` Hermes plugin (Python half + Desktop settings page) that lets the main agent consult a second model, optionally review every turn, gate completion, and resolve forks with Jev.

**Architecture:** One Python package registers two tools, one slash command and four hooks on the host `PluginContext`. A pure-Python core (`coach/`) holds the ledger, findings, state, delivery routing and LLM/HTTP clients behind injectable seams, so every unit is testable without a live turn. The Desktop half is a plain-ESM `desktop/plugin.js` settings page backed by `dashboard/plugin_api.py`.

**Tech Stack:** Python 3.14 (stdlib only at runtime; `fastapi`/`pydantic` come from Hermes), pytest for tests, plain ESM JS for the Desktop half.

**Spec:** `docs/superpowers/specs/2026-11-05-hermes-coach-design.md`

## Global Constraints

- Plugin id `coach`; tools `coach_ask`, `coach_decide`; slash command `/coach`.
- Tool handlers return `str` (JSON), never a dict.
- Hooks never raise: every failure is logged at DEBUG and swallowed.
- No new runtime dependencies. `urllib.request` for HTTP; `subprocess` with list argv and `shell=False`; `pathlib` for paths. Cross-platform (Linux, macOS, Windows).
- Writes confined to `$HERMES_HOME/coach/` and the two guidance files; nothing else.
- Settings keys are flat snake_case. Defaults live in `coach/settings.py`; `plugin.yaml` mirrors them and a test enforces equality.
- No network at import time; no work in `register()` beyond registration.
- All user-facing strings in English, sentence case.
- Test command: `./scripts/test.sh` = `uv run --python /home/lenovo/.hermes/hermes-agent/.venv/bin/python --with pytest python -m pytest tests -q`.

## Review Focus

1. **Nothing configured yet.** Every tool and hook must return an explanatory line, not an exception, when `coach_model` is empty. Test in Task 6.
2. **Not a git repo / new untracked files.** Review evidence must include untracked file heads and degrade to file reads; it must never report "reviewed" with empty evidence. Test in Task 9.
3. **Provider error, timeout, or silent fallback to the primary model.** The turn must proceed, the reply must be discarded, and the failure recorded. Test in Task 5.
4. **Enormous turn.** A 200-message history with 100k-char tool results must assemble under the ledger ceiling with every user message intact. Test in Task 3.
5. **Desktop page before permissions or plugin enable.** Status must render "unconfigured" and "not granted", never blank or 500. Test in Task 12.

## File Structure

```
plugin.yaml                     # manifest + config_schema (mirrors coach/settings.py)
__init__.py                     # register(ctx): wire hooks, tools, command
coach/__init__.py
coach/settings.py               # Settings, DEFAULTS, load_settings, model_for, is_configured
coach/state.py                  # SessionState, Store (atomic JSON, prune)
coach/ledger.py                 # turn records, message assembly, token estimate
coach/findings.py               # Finding, JSON schema, parse, digest
coach/advisor.py                # Coach: model resolution, call, deadline, fallback, cost
coach/evidence.py               # bounded git diff + untracked heads
coach/jev.py                    # three decide backends
coach/delivery.py               # routing + text formatters
coach/worker.py                 # single-flight review queue
coach/plugin.py                 # CoachPlugin: tools, hooks, command, status
coach/guidance.py               # read/write global + project COACH.md
dashboard/manifest.json
dashboard/plugin_api.py         # status, config, guidance, permissions
desktop/plugin.js               # registerSettingsPage
scripts/test.sh
tests/                          # one file per module
```

---

### Task 1: Package scaffold, settings, manifest

**Files:**
- Create: `plugin.yaml`, `__init__.py`, `coach/__init__.py`, `coach/settings.py`, `scripts/test.sh`
- Test: `tests/test_settings.py`, `tests/test_manifest.py`

**Interfaces:**
- Produces: `coach.settings.DEFAULTS: dict[str, Any]`, `load_settings(raw: Mapping[str, Any]) -> Settings`, `Settings.model_for(mode: str) -> ModelChoice | None`, `Settings.is_configured() -> bool`, `Settings.same_as_primary(choice: ModelChoice, model: str, provider: str) -> bool`, `ModelChoice(provider: str, model: str)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settings.py
from coach.settings import DEFAULTS, load_settings

def test_defaults_are_unconfigured():
    s = load_settings({})
    assert s.is_configured() is False
    assert s.model_for("consult") is None
    assert s.active_enabled is False
    assert s.review_enabled is True
    assert s.delivery_policy == "severity"

def test_per_mode_override_inherits_default():
    s = load_settings({"coach_model": {"provider": "openrouter", "model": "a/b"}})
    assert s.is_configured() is True
    assert s.model_for("consult").model == "a/b"
    s2 = load_settings({"coach_model": {"provider": "openrouter", "model": "a/b"},
                        "review_model": {"provider": "anthropic", "model": "c"}})
    assert s2.model_for("review").provider == "anthropic"
    assert s2.model_for("active").model == "a/b"

def test_bad_values_fall_back_to_defaults():
    s = load_settings({"active_every_n": "nope", "decide_min_confidence": 3.0,
                       "budget_on_exceed": "explode"})
    assert s.active_every_n == DEFAULTS["active_every_n"]
    assert s.decide_min_confidence == 1.0
    assert s.budget_on_exceed == "soft"
```

```python
# tests/test_manifest.py
import yaml
from coach.settings import DEFAULTS

def test_manifest_mirrors_defaults():
    manifest = yaml.safe_load(open("plugin.yaml"))
    schema = manifest["config_schema"]
    assert manifest["name"] == "coach"
    for key, default in DEFAULTS.items():
        assert key in schema, key
        assert schema[key].get("default") == default, key

def test_every_schema_key_has_a_description():
    schema = yaml.safe_load(open("plugin.yaml"))["config_schema"]
    assert all(len(str(spec.get("description", "")).strip()) > 20 for spec in schema.values())
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh`
Expected: collection error, `No module named 'coach'`.

- [ ] **Step 3: Implement**

`plugin.yaml`: `name: coach`, `version: "0.1.0"`, a description that names all four modes, `author: Nous user`, `kind: standalone`, `provides_hooks: [post_llm_call, pre_llm_call, pre_verify, post_tool_call, subagent_start, subagent_stop, on_session_end]`, and `config_schema` with every key from the spec's §9 tables. Types: `str|int|float|bool|json|secret`; use `choices:` for enums. Copy each description verbatim from §9. `coach_model` default is `null`.

`coach/settings.py`: a frozen `ModelChoice` dataclass, a frozen `Settings` dataclass with one field per key, `DEFAULTS` holding the same values, and `load_settings` coercing with `_as_bool/_as_int/_as_float/_as_choice` helpers that fall back to the default on any bad value (clamp confidence to 0.0–1.0). `model_for` returns the mode override or `coach_model`. `same_as_primary` compares both model and provider, case-insensitively.

`__init__.py` at Task 1 is a stub `def register(ctx): pass`; it gains wiring in Task 6.

`scripts/test.sh`:

```sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --python /home/lenovo/.hermes/hermes-agent/.venv/bin/python --with pytest python -m pytest tests -q "$@"
```

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugin.yaml __init__.py coach scripts tests
git commit -m "feat: scaffold coach plugin with settings contract"
```

---

### Task 2: Session state store

**Files:**
- Create: `coach/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `SessionState` dataclass with `session_id`, `ledger: list[dict]`, `note: str`, `pending: list[dict]`, `note_seq: int`, `holds: int`, `spend_usd: float`, `calls: dict[str, int]`, `last_error: str`, `updated_at: float`; `Store(root: Path, max_sessions: int = 20)` with `load(session_id) -> SessionState`, `save(state)`, `prune()`, `forget(session_id)`, `spend_total() -> float`, `sessions_on_disk() -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_state.py
import json, os
from pathlib import Path
from coach.state import SessionState, Store

def test_roundtrip_and_defaults(tmp_path):
    store = Store(tmp_path)
    s = store.load("s1")
    assert s.session_id == "s1" and s.spend_usd == 0.0 and s.ledger == []
    s.note = "ship the plugin"
    store.save(s)
    assert store.load("s1").note == "ship the plugin"

def test_atomic_write_leaves_no_tmp(tmp_path):
    store = Store(tmp_path); s = store.load("s1"); store.save(s)
    assert [p.name for p in tmp_path.iterdir()] == ["s1.json"]

def test_prune_keeps_newest_twenty(tmp_path):
    store = Store(tmp_path, max_sessions=20)
    for i in range(25):
        s = store.load(f"s{i:02d}"); s.updated_at = float(i); store.save(s)
    store.prune()
    assert len(store.sessions_on_disk()) == 20
    assert "s00" not in store.sessions_on_disk()

def test_corrupt_file_is_reset_not_raised(tmp_path):
    (tmp_path / "bad.json").write_text("{nope")
    s = Store(tmp_path).load("bad")
    assert s.ledger == [] and s.last_error
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_state.py`
Expected: `ImportError: cannot import name 'Store'`.

- [ ] **Step 3: Implement**

`load` returns a fresh state on missing/corrupt files (recording `last_error="state reset"`), coerces types, and never raises. `save` writes `<tmp>` in the same directory with `os.replace`, mode `0o600`, creating the dir with `0o700`. `prune` sorts by `updated_at` and unlinks the oldest beyond the cap. `forget` unlinks and swallows `FileNotFoundError`.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_state.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/state.py tests/test_state.py
git commit -m "feat: add session state store"
```

---

### Task 3: Ledger compiler

**Files:**
- Create: `coach/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Produces: `append_turn(state: SessionState, *, turn_id: str, user: str, assistant: str, tools: list[dict]) -> None`; `tool_card(name: str, arg: str, status: str, size: int, preview: str, *, max_chars: int) -> dict`; `build_messages(*, system: str, guidance: str, state: SessionState, question: str | None, max_tokens: int, tool_result_chars: int, recent_turns: int) -> list[dict]`; `estimate_tokens(text: str) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_ledger.py`
Expected: `ModuleNotFoundError: coach.ledger`.

- [ ] **Step 3: Implement**

Message order: `system` (system prompt + guidance + ledger note), then one `user` message holding the rendered ledger, then the question as the final `user` block when present. Rendering: one line per turn heading (`[turn N]`), the user message verbatim, assistant final answer truncated to 600 chars, then tool cards. Assembly drops in this order until under `max_tokens * 4` characters: oldest tool cards, old assistant text, old turn headings. User messages are never dropped; if they alone exceed the ceiling, keep them and stop adding anything else. `tool_card` truncates preview to `max_chars` and records `size`.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_ledger.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/ledger.py tests/test_ledger.py
git commit -m "feat: add bounded ledger compiler"
```

---

### Task 4: Findings schema and parser

**Files:**
- Create: `coach/findings.py`
- Test: `tests/test_findings.py`

**Interfaces:**
- Produces: `SEVERITIES = ("none","nit","concern","blocker")`; `Finding(severity,title,body,evidence,fix)` with `digest() -> str`; `SCHEMA: dict` (JSON schema); `parse_finding(parsed: Any, text: str = "") -> Finding | None`; `is_deliverable(f: Finding | None) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_findings.py
from coach.findings import Finding, is_deliverable, parse_finding

GOOD = {"severity": "concern", "title": "Unbounded retry", "body": "Loop has no cap.",
        "evidence": "client.py:88", "fix": "Cap attempts at 3."}

def test_parses_valid_payload():
    f = parse_finding(GOOD)
    assert f.severity == "concern" and f.fix.startswith("Cap")

def test_none_and_garbage_are_not_deliverable():
    assert parse_finding({"severity": "none"}) is None
    assert parse_finding(None, "I think it is fine") is None
    assert parse_finding({"severity": "banana", "title": "x"}) is None
    assert is_deliverable(None) is False

def test_digest_ignores_whitespace_and_case():
    a = Finding("nit", "Spacing", "body", "a.py:1", "fix")
    b = Finding("nit", "  spacing ", "body", "A.PY:1 ", "fix")
    assert a.digest() == b.digest()

def test_title_and_body_are_bounded():
    f = parse_finding({**GOOD, "title": "t" * 500, "body": "b" * 4000})
    assert len(f.title) <= 80 and len(f.body) <= 600
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_findings.py`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`SCHEMA` requires `severity` (enum), `title`, `body`, `evidence`, `fix`, with `additionalProperties: False`. `parse_finding` accepts a dict (from structured output) or falls back to scanning `text` for a fenced JSON object; anything that does not yield a valid severity, title and body returns `None` — never a fabricated finding. `severity == "none"` returns `None`. `digest` is `sha1(f"{severity}|{normalised title}|{normalised evidence}")`.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_findings.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/findings.py tests/test_findings.py
git commit -m "feat: add structured finding parser"
```

---

### Task 5: Coach client

**Files:**
- Create: `coach/advisor.py`
- Test: `tests/test_advisor.py`

**Interfaces:**
- Produces: `CoachReply(text, finding, provider, model, usage, cost_usd, error)`; `Coach(llm, settings, *, primary_model="", primary_provider="", cost_fn=None, clock=time.time)`; `Coach.ask(messages, *, mode: str, structured: bool = False, timeout: float = 45.0) -> CoachReply`; `Coach.check() -> str` (returns `""` when usable, else the reason).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_advisor.py
from coach.advisor import Coach
from coach.settings import load_settings

class Usage:
    input_tokens = 1000; output_tokens = 100
    cache_read_tokens = 0; cache_write_tokens = 0

class Result:
    def __init__(self, text, provider="openrouter", model="x", parsed=None):
        self.text, self.provider, self.model, self.parsed = text, provider, model, parsed
        self.usage, self.audit = Usage(), {}
        self.content_type = "json" if parsed else "text"

class FakeLlm:
    def __init__(self, result): self.result = result; self.calls = []
    def complete(self, messages, **kw):
        self.calls.append(kw); return self.result
    def complete_structured(self, **kw):
        self.calls.append(kw); return self.result

def _settings(**kw):
    base = {"coach_model": {"provider": "openrouter", "model": "x"}}
    base.update(kw); return load_settings(base)

def test_unconfigured_reports_and_makes_no_call():
    llm = FakeLlm(Result("hi"))
    r = Coach(llm, load_settings({})).ask([{"role": "user", "content": "q"}], mode="consult")
    assert r.error == "unconfigured" and llm.calls == []

def test_passes_model_and_provider_and_prices_the_call():
    llm = FakeLlm(Result("advice"))
    coach = Coach(llm, _settings(), cost_fn=lambda model, usage, provider: 0.0042)
    r = coach.ask([{"role": "user", "content": "q"}], mode="consult")
    assert r.text == "advice" and r.cost_usd == 0.0042
    assert llm.calls[0]["model"] == "x" and llm.calls[0]["provider"] == "openrouter"

def test_fallback_to_primary_is_discarded():
    llm = FakeLlm(Result("advice", provider="openrouter", model="primary"))
    coach = Coach(llm, _settings(), primary_model="primary", primary_provider="openrouter")
    r = coach.ask([{"role": "user", "content": "q"}], mode="consult")
    assert r.error == "fallback_mismatch" and r.text == ""

def test_same_model_as_primary_is_refused():
    coach = Coach(FakeLlm(Result("x")), _settings(), primary_model="x", primary_provider="openrouter")
    assert coach.check() == "same_model"

def test_provider_error_is_returned_not_raised():
    class Boom:
        def complete(self, *a, **k): raise RuntimeError("503")
    r = Coach(Boom(), _settings()).ask([{"role": "user", "content": "q"}], mode="consult")
    assert r.error.startswith("provider_error") and r.text == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_advisor.py`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`check()` returns `"unconfigured"`, `"same_model"` (when `skip_same_model` and the resolved choice matches the primary and the mode is not explicitly overridden), or `""`. `ask()` resolves the model for `mode`, returns a `CoachReply(error=...)` without calling when `check()` fails, calls `complete_structured(instructions=..., input=[{"type":"text","text": body}], json_schema=SCHEMA, system_prompt=system)` for structured and `complete(messages)` otherwise, and maps exceptions to `provider_error: <ExceptionType>`. After the call, if `result.provider`/`result.model` differ from the requested pair, return `fallback_mismatch`. Cost: `cost_fn(model, result.usage, provider)` when injected, else `agent.usage_pricing.estimate_usage_cost(...).amount_usd`, guarded by try/except to `0.0`.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_advisor.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/advisor.py tests/test_advisor.py
git commit -m "feat: add coach LLM client with fail-open errors"
```

---

### Task 6: Consult tool, registration, unconfigured state

**Files:**
- Create: `coach/plugin.py`, `coach/guidance.py`; modify `__init__.py`
- Test: `tests/test_plugin_ask.py`

**Interfaces:**
- Produces: `CoachPlugin(*, llm, store, settings, primary_model="", primary_provider="", post=None, inject=None, surface=lambda: "cli", clock=time.time, cost_fn=None, guidance=None, set_setting=None)`; `.tool_ask(args: dict) -> str`; `.status() -> dict`; `build_plugin(ctx) -> CoachPlugin`; `Guidance(root: Path)` with `.system_block(cwd: str) -> str`, `.read(scope, cwd) -> str`, `.write(scope, cwd, text) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_plugin_ask.py
import json
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store

class Llm:
    def __init__(self): self.last = None
    def complete(self, messages, **kw):
        self.last = (messages, kw)
        class R:
            text = "Consider extracting the parser."; provider = "openrouter"; model = "x"
            usage = type("U", (), {"input_tokens": 10, "output_tokens": 5,
                                   "cache_read_tokens": 0, "cache_write_tokens": 0})()
            audit = {}
        return R()

def _plugin(tmp_path, *, settings=None, surface=lambda: "cli"):
    return CoachPlugin(llm=Llm(), store=Store(tmp_path), surface=surface,
                       settings=settings or load_settings(
                           {"coach_model": {"provider": "openrouter", "model": "x"}}))

def test_ask_returns_text_as_json_string(tmp_path):
    out = json.loads(_plugin(tmp_path).tool_ask({"question": "Is this parser ok?", "intent": "advice"}))
    assert out["ok"] is True and out["advice"].startswith("Consider")

def test_unconfigured_answers_clearly(tmp_path):
    p = CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings({}))
    out = json.loads(p.tool_ask({"question": "q"}))
    assert out["ok"] is False and "no coach model" in out["error"]
    assert p.status()["configured"] is False

def test_child_sessions_are_refused(tmp_path):
    p = _plugin(tmp_path, surface=lambda: "subagent")
    out = json.loads(p.tool_ask({"question": "q"}))
    assert out["ok"] is False and out["error"] == "child_session"

def test_per_turn_cap(tmp_path):
    p = _plugin(tmp_path)
    args = {"question": "q", "turn_id": "t1"}
    for _ in range(4):
        assert json.loads(p.tool_ask(args))["ok"] is True
    assert json.loads(p.tool_ask(args))["error"] == "call_cap"

def test_budget_hard_stop_refuses_consult(tmp_path):
    p = _plugin(tmp_path, settings=load_settings(
        {"coach_model": {"provider": "openrouter", "model": "x"},
         "budget_session_usd": 0.01, "budget_on_exceed": "hard"}))
    st = p.store.load("current"); st.spend_usd = 1.0; p.store.save(st)
    assert json.loads(p.tool_ask({"question": "q"}))["error"] == "budget_exhausted"
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_plugin_ask.py`
Expected: `ModuleNotFoundError: coach.plugin`.

- [ ] **Step 3: Implement**

`tool_ask` validates `question` (non-empty, ≤4000 chars), resolves `intent` in `{"advice","plan","review"}`, refuses child sessions (`surface.get()` in `{"subagent","delegate"}` unless `settings.allow_in_subagents`), refuses with `budget_exhausted` when `budget_on_exceed` is `hard` and session spend has reached the budget, enforces `consult_max_calls_per_turn` keyed by `turn_id` (default `"current"`), builds messages with `build_messages`, calls `Coach.ask(..., structured=False)`, appends the exchange to state, and returns `json.dumps({...})`. Errors are JSON with `ok: false`, never exceptions.

`build_plugin(ctx)` reads each settings key with `ctx.get_config(key, default)` (iterate `DEFAULTS`), resolves the state root as `Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "coach"`, and passes `llm=ctx.llm`, `post=ctx.post_message`, `inject=ctx.inject_message`, `surface=ctx.session_surface`.

`__init__.py` wires: `register_tool("coach_ask", "coach", ASK_SCHEMA, plugin.tool_ask, description=...)`, the hooks from later tasks (register only what exists yet), `register_command("coach", plugin.command)` once Task 11 lands. Registration must be wrapped so a duplicate/absent API logs and continues.

`guidance.system_block`: global file `$HERMES_HOME/coach/guidance.md`, then `<cwd>/COACH.md` when `guidance_enabled`; both capped at 4000 chars each; wrapped in `<coach-guidance>` tags.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_plugin_ask.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/plugin.py coach/guidance.py coach/worker.py __init__.py tests/test_plugin_ask.py
git commit -m "feat: register coach_ask with unconfigured and child-session guards"
```

---

### Task 7: Delivery routing

**Files:**
- Create: `coach/delivery.py`; modify `coach/plugin.py`
- Test: `tests/test_delivery.py`, `tests/test_plugin_delivery.py`

**Interfaces:**
- Produces: `route(finding, settings, *, surface: str, holds_used: int) -> str` in `{"ambient","corrective","drop"}`; `format_ambient(f, turn_id) -> str`; `format_corrective(f) -> str`; `format_card(f) -> str`; `CoachPlugin.on_pre_llm_call(**kw) -> dict | None`; `.deliver(finding, *, session_id, turn_id)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_delivery.py
from coach.delivery import format_ambient, route
from coach.findings import Finding
from coach.settings import load_settings

F = Finding("blocker", "Data loss", "Deletes before copy.", "store.py:40", "Copy first.")

def test_severity_policy_routes_blockers_corrective():
    s = load_settings({"delivery_policy": "severity"})
    assert route(F, s, surface="cli", holds_used=0) == "corrective"
    nit = Finding("nit", "t", "b", "e", "f")
    assert route(nit, s, surface="cli", holds_used=0) == "ambient"

def test_corrective_cap_falls_back_to_ambient():
    s = load_settings({"delivery_policy": "severity", "delivery_max_corrective_per_request": 1})
    assert route(F, s, surface="cli", holds_used=1) == "ambient"

def test_messaging_surfaces_never_inject():
    s = load_settings({"delivery_policy": "corrective"})
    assert route(F, s, surface="telegram", holds_used=0) == "ambient"

def test_ambient_block_is_attributed_and_weighs():
    text = format_ambient(F, "turn-7")
    assert "coach-note" in text and "blocker" in text and "weigh" in text.lower()
    assert "store.py:40" in text
```

```python
# tests/test_plugin_delivery.py
import json
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.test_plugin_ask import Llm

def _p(tmp_path, **cfg):
    base = {"coach_model": {"provider": "p", "model": "m"}}
    base.update(cfg)
    posted, injected = [], []
    p = CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings(base),
                    post=posted.append, inject=lambda t: injected.append(t) or True)
    p._test_post, p._test_inject = posted, injected
    return p

def test_pending_note_is_delivered_once(tmp_path):
    p = _p(tmp_path)
    p.deliver(type("F", (), {"severity": "nit", "title": "t", "body": "b",
                             "evidence": "e", "fix": "f"})(), session_id="s", turn_id="t1")
    first = p.on_pre_llm_call(session_id="s", turn_id="t2", conversation_history=[])
    second = p.on_pre_llm_call(session_id="s", turn_id="t3", conversation_history=[])
    assert first and "t" in first["context"]
    assert second is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_delivery.py tests/test_plugin_delivery.py`
Expected: `ModuleNotFoundError: coach.delivery`.

- [ ] **Step 3: Implement**

`route` applies: messaging surface → ambient; policy `ambient` → ambient; `corrective` → corrective unless capped; `severity` → blocker corrective unless capped, else ambient. `deliver` computes the route, posts a card when `delivery_show_cards`, stores ambient notes in `state.pending` with `turn_id` and `expires_at_turn = seq + ttl`, and calls `inject(format_corrective(f))` for corrective. `on_pre_llm_call` pops every unexpired pending note for the session and returns `{"context": "\n".join(...)}` or `None`; it also increments the turn sequence so TTL works.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_delivery.py tests/test_plugin_delivery.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/delivery.py coach/plugin.py tests/test_delivery.py tests/test_plugin_delivery.py
git commit -m "feat: deliver coach notes ambient or corrective per policy"
```

---

### Task 8: Active mode

**Files:**
- Create: `coach/worker.py`; modify `coach/plugin.py`; Test: `tests/test_active.py`

**Interfaces:**
- Produces: `Worker(run: Callable[[str], None])` with `.submit(session_id) -> bool`, `.drain(timeout)`, `.stop()` — one in-flight, newest queued wins; `CoachPlugin.on_post_tool_call(**kw) -> None`; `.on_post_llm_call(**kw) -> None`; `.run_review(session_id, turn_id) -> None`; `.should_review(state, turn, settings) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_active.py
import json
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.test_plugin_ask import Llm

class FakeWorker:
    def __init__(self): self.submitted = []
    def submit(self, session_id): self.submitted.append(session_id); return True

def _p(tmp_path, **cfg):
    base = {"coach_model": {"provider": "p", "model": "m"}, "active_enabled": True}
    base.update(cfg)
    p = CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings(base))
    p.worker = FakeWorker()
    return p

TURN = dict(session_id="s", turn_id="t1", user_message="fix the bug",
            assistant_response="done", conversation_history=[], model="primary")

def test_chat_only_turn_is_skipped(tmp_path):
    p = _p(tmp_path)
    p.on_post_llm_call(**TURN)
    assert p.worker.submitted == []
    assert p.should_review(p.store.load("s"), {"tools": []}, p.settings) is False

def test_tool_turn_is_queued_once(tmp_path):
    p = _p(tmp_path)
    p.on_post_tool_call(session_id="s", turn_id="t1", tool_name="read_file", args={}, result="ok")
    p.on_post_llm_call(**TURN)
    p.on_post_llm_call(**TURN)
    assert p.worker.submitted == ["s"]

def test_disabled_mode_never_queues(tmp_path):
    p = _p(tmp_path, active_enabled=False)
    p.on_post_tool_call(session_id="s", turn_id="t1", tool_name="x", args={}, result="")
    p.on_post_llm_call(**TURN)
    assert p.worker.submitted == []

def test_budget_soft_stop_blocks_active(tmp_path):
    p = _p(tmp_path, budget_session_usd=0.01, budget_on_exceed="soft")
    st = p.store.load("s"); st.spend_usd = 1.0; p.store.save(st)
    p.on_post_tool_call(session_id="s", turn_id="t1", tool_name="x", args={}, result="")
    p.on_post_llm_call(**TURN)
    assert p.worker.submitted == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_active.py`
Expected: no `on_post_llm_call` or assertion failures.

- [ ] **Step 3: Implement**

`on_post_tool_call` increments `state.calls[turn_id]` and records a tool card on the current turn record. `on_post_llm_call` only acts when `active_enabled`, cadence permits (`tool_turns_only` requires ≥1 tool card; `every_n` requires `turns_since_review >= every_n`; `every_turn` always), the turn is not identical to the last reviewed digest, budget is not exhausted, and a review is not already queued for this session. It snapshots the turn into state and calls `worker.submit`. `run_review` builds messages, calls `Coach.ask(mode="active")`, parses the finding, records `spend_usd` and one JSONL log line, then `deliver`. Every exit path saves state and swallows exceptions.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_active.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/plugin.py tests/test_active.py
git commit -m "feat: add active turn review with cadence and budget guards"
```

---

### Task 9: Review gate

**Files:**
- Create: `coach/evidence.py`; modify `coach/plugin.py`
- Test: `tests/test_evidence.py`, `tests/test_review_gate.py`

**Interfaces:**
- Produces: `collect(cwd: str, paths: list[str], *, max_chars: int = 8000, run=None, read=None) -> str`; `CoachPlugin.on_pre_verify(**kw) -> dict | None`; `.collect_evidence(cwd: str, paths: list[str]) -> str` (defaults to `evidence.collect`, replaced in tests); `.review_now(**kw) -> Finding | None` (the single structured call, replaced in tests).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evidence.py
import subprocess
from coach.evidence import collect

def test_untracked_files_are_included(tmp_path, monkeypatch):
    (tmp_path / "new.py").write_text("print('hi')\n")
    calls = []
    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "?? new.py\n", "")
    text = collect(str(tmp_path), ["new.py"], run=fake_run)
    assert "(new file)" in text and "print('hi')" in text

def test_non_repo_falls_back_to_file_heads(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, "", "not a git repository")
    text = collect(str(tmp_path), ["a.py"], run=fake_run)
    assert "x = 1" in text

def test_budget_is_respected(tmp_path):
    (tmp_path / "big.py").write_text("y" * 50000)
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 128, "", "")
    assert len(collect(str(tmp_path), ["big.py"], max_chars=500)) <= 600
```

```python
# tests/test_review_gate.py
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.test_plugin_ask import Llm

def _p(tmp_path, **cfg):
    base = {"coach_model": {"provider": "p", "model": "m"}}
    base.update(cfg)
    return CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings(base))

PAY = dict(session_id="s", turn_id="t1", final_response="All done.",
           changed_paths=["a.py"], model="primary", coding=True, attempt=0)

def test_review_skipped_when_disabled(tmp_path):
    p = _p(tmp_path, review_enabled=False)
    assert p.on_pre_verify(**PAY) is None

def test_report_authority_never_holds(tmp_path):
    p = _p(tmp_path, review_authority="report")
    p.collect_evidence = lambda cwd, paths: "diff --git a/a.py"
    out = p.on_pre_verify(**PAY)
    assert out is None or "action" not in out

def test_hold_is_capped_per_request(tmp_path):
    p = _p(tmp_path, review_authority="hold_once")
    p.collect_evidence = lambda cwd, paths: "diff --git a/a.py"
    p.review_now = lambda **kw: type("F", (), {"severity": "blocker", "title": "t",
                                               "body": "b", "evidence": "e", "fix": "f"})()
    first = p.on_pre_verify(**PAY)
    second = p.on_pre_verify(**PAY)
    assert first["action"] == "continue"
    assert second is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_evidence.py tests/test_review_gate.py`
Expected: `ModuleNotFoundError` / missing attribute.

- [ ] **Step 3: Implement**

`collect` runs `git status --porcelain` and `git diff HEAD -- <paths>` (list argv, `shell=False`, `timeout=5`, `cwd=tmp`), appends `(new file)` heads for untracked paths, truncates to `max_chars`, and on any non-zero git exit falls back to reading each path under `cwd` (rejecting absolute paths and `..`). `on_pre_verify` gates on `review_enabled`, `changed_paths`, and remaining holds; assembles `{request: last user message, claim: final_response, evidence}`; calls `Coach.ask(mode="review", timeout=20)`; on blocker with `review_authority="hold_once"` increments holds and returns `{"action": "continue", "message": "Coach: <title>. <fix> Fix or explain before claiming done."}`; otherwise posts a card and returns `None`.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_evidence.py tests/test_review_gate.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/evidence.py coach/plugin.py tests/test_evidence.py tests/test_review_gate.py
git commit -m "feat: gate completion on a bounded diff review"
```

---

### Task 10: Decide tool

**Files:**
- Create: `coach/jev.py`; modify `coach/plugin.py`
- Test: `tests/test_jev.py`, `tests/test_plugin_decide.py`

**Interfaces:**
- Produces: `Decision(decision, confidence, probabilities, gate, rationale, cost_usd)`; `decide(*, backend, question, options, state, model="typesafe/jev-1.14", api_key="", base_url="", min_confidence=0.6, http=None, chat=None) -> Decision`; `CoachPlugin.tool_decide(args: dict) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_jev.py
import json
from coach.jev import decide

class Resp:
    def __init__(self, payload): self.payload = payload
    def read(self): return json.dumps(self.payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False

def test_openrouter_choice_is_parsed():
    body = {"answers": {"path": {"type": "choice", "choice": "b"}},
            "verdicts": {"path": {"gate": "act", "confidence": 0.82}},
            "usage": {"cost": 0.0004}}
    d = decide(backend="jev-openrouter", question="Which?", options=["a", "b"],
               state={"goal": "x"}, api_key="k", http=lambda req, **kw: Resp(body))
    assert d.decision == "b" and d.gate == "act" and d.confidence == 0.82

def test_low_confidence_escalates():
    body = {"answers": {"path": {"choice": "a"}},
            "verdicts": {"path": {"gate": "act", "confidence": 0.31}}, "usage": {}}
    d = decide(backend="jev-openrouter", question="q", options=["a", "b"],
               state={}, api_key="k", min_confidence=0.6, http=lambda r, **k: Resp(body))
    assert d.gate == "escalate"

def test_missing_key_is_an_error_not_a_call():
    d = decide(backend="jev-openrouter", question="q", options=["a"], state={}, api_key="")
    assert d.gate == "error" and d.rationale
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_jev.py`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Build `{model, state, questions: {choice: {type: "choice", instructions: question, criteria: {opt: opt}}}}`; POST JSON with `Authorization: Bearer <key>` and `Content-Type: application/json` to `https://openrouter.ai/api/alpha/decisions` (openrouter) or `https://api.typesafe.ai/v1/systemone` (direct), via `urllib.request.urlopen` with `timeout=15`. Parse `answers.*.choice` and `verdicts.*.{gate,confidence}`; force `escalate` below `min_confidence`; return `gate="error"` with a one-line reason on any failure. The `chat-model` backend calls `Coach.ask(structured=True)` with a choice schema and maps the result.

`tool_decide` validates 2–6 options, enforces `decide_max_calls_per_turn`, resolves the backend/key (`TYPESAFE_API_KEY` or `OPENROUTER_API_KEY` from env), and returns the Decision as JSON with `ok`.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_jev.py tests/test_plugin_decide.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/jev.py coach/plugin.py tests/test_jev.py tests/test_plugin_decide.py
git commit -m "feat: add coach_decide with jev and chat-model backends"
```

---

### Task 11: `/coach` command and status

**Files:**
- Modify: `coach/plugin.py`, `__init__.py`
- Test: `tests/test_command.py`

**Interfaces:**
- Produces: `CoachPlugin.command(raw: str) -> str`; `.status() -> dict`; `__init__.py` registers `ctx.register_command("coach", plugin.command, description="Coach settings and status")` and a `command_args`-free handler that parses raw text.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_command.py
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.test_plugin_ask import Llm

def _p(tmp_path):
    return CoachPlugin(llm=Llm(), store=Store(tmp_path),
                       settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}))

def test_status_lists_modes_and_spend(tmp_path):
    out = _p(tmp_path).command("status")
    assert "Active" in out and "Review" in out and "$" in out

def test_unconfigured_status_explains(tmp_path):
    p = CoachPlugin(llm=Llm(), store=Store(tmp_path), settings=load_settings({}))
    assert "Unconfigured" in p.command("status")

def test_set_toggles_persist_through_callback(tmp_path):
    saved = {}
    p = CoachPlugin(llm=Llm(), store=Store(tmp_path),
                    settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
                    set_setting=lambda k, v: saved.__setitem__(k, v))
    assert "off" in p.command("active off").lower()
    assert saved["active_enabled"] is False

def test_unknown_subcommand_prints_help(tmp_path):
    assert "/coach" in _p(tmp_path).command("wat")
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_command.py`
Expected: `AttributeError: command`.

- [ ] **Step 3: Implement**

Subcommands: `status`, `active on|off`, `review on|off`, `decide on|off`, `model [mode] [provider/model]`, `set <key> <value>`, `guidance`, `pending`, `send`, `pause`, `grant`. `set` validates against `DEFAULTS` keys and calls `set_setting(key, coerced)`; without a model argument, `model` prints the current selection and the resolved per-mode models. `pending` lists notes; `send` pushes them through `inject`. Every unknown subcommand prints the full help.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_command.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add coach/plugin.py __init__.py tests/test_command.py
git commit -m "feat: add /coach command surface"
```

---

### Task 12: Dashboard API and Desktop settings page

**Files:**
- Create: `dashboard/manifest.json`, `dashboard/plugin_api.py`, `desktop/plugin.js`
- Test: `tests/test_dashboard_api.py`

**Interfaces:**
- Produces: routes `GET /status`, `GET /config`, `GET /guidance?scope=&cwd=`, `PUT /guidance`, `POST /permissions/grant`; `plugin_api.router`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dashboard_api.py
from fastapi.testclient import TestClient
from dashboard.plugin_api import router, app_for_tests

def test_status_reports_unconfigured(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    body = client.get("/status").json()
    assert body["configured"] is False
    assert body["permissions"]["model_overrides"] is False

def test_guidance_roundtrip(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    project = tmp_path / "proj"; project.mkdir()
    r = client.put("/guidance", json={"scope": "project", "cwd": str(project), "text": "no regex"})
    assert r.status_code == 200
    assert client.get("/guidance", params={"scope": "project", "cwd": str(project)}).json()["text"] == "no regex"

def test_guidance_rejects_unregistered_path(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    r = client.put("/guidance", json={"scope": "project", "cwd": "/etc", "text": "x"})
    assert r.status_code == 400
```

- [ ] **Step 2: Run to verify it fails**

Run: `./scripts/test.sh tests/test_dashboard_api.py`
Expected: `ModuleNotFoundError: dashboard`.

- [ ] **Step 3: Implement**

`manifest.json`: `{name, label: "Coach", icon: "sparkle", version: "0.1.0", api: "plugin_api.py"}`. `plugin_api.py` exposes `router = APIRouter()` plus `app_for_tests(home: Path)` building a FastAPI app with that router. Project guidance writes only to `<cwd>/COACH.md` when `cwd` is an existing directory recorded in `$HERMES_HOME/coach/state.json`'s `seen_cwds` (append on every hook), never creates directories, caps 8000 chars. Grant writes `allow_provider_override`/`allow_model_override` via `load_config()` → mutate → `save_config()`, then returns the new status.

`desktop/plugin.js`: `ctx.registerSettingsPage({id, title, icon, render, children})` with sub-pages Models (picker from `host.request("model.options")`, per-mode inherit), Modes (switches + selects), Delivery, Budget, Guidance (scope switch, textarea, char/token count, save via `ctx.rest`), Status (configured, spend, pending, permission row with Grant). All controls use `useValue`/`jsx`; no build step.

- [ ] **Step 4: Run to verify it passes**

Run: `./scripts/test.sh tests/test_dashboard_api.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard desktop tests/test_dashboard_api.py
git commit -m "feat: add settings API and desktop settings page"
```

---

### Task 13: Install and live verification

**Files:**
- Modify: `README.md` (create), no code changes expected
- Test: live probes

- [ ] **Step 1: Install by symlink and enable**

```bash
ln -sfn /home/lenovo/Development/Hermes-Coach /home/lenovo/.hermes/plugins/coach
hermes plugins doctor /home/lenovo/Development/Hermes-Coach
```

Expected: manifest and hooks validated; no security findings.

- [ ] **Step 2: Enable and check the tool is dispatched by the real host**

```bash
hermes plugins enable coach
hermes -z "Call the coach_ask tool once with question 'reply with OK' and report its exact JSON result. Do not do anything else." 2>&1 | tail -20
```

Expected: the tool appears in the run and returns the unconfigured JSON (no coach model set). This proves registration and the `str` result contract through the real dispatcher — a green pytest suite does not.

- [ ] **Step 3: Check the hooks fire**

```bash
HERMES_PLUGINS_DEBUG=1 hermes -z "Print the word done." 2>&1 | grep -i "coach" | head
```

Expected: no traceback; with `debug_log_decisions: true`, one ledger line.

- [ ] **Step 4: Desktop page**

Open Settings → Plugins → Coach (after toggling the Desktop half on), set a coach model, hit Grant, save guidance, and confirm the status tab shows configured with $0.00 spend.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: install and verify the coach plugin"
```
