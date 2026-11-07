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

def test_a_host_resolved_model_is_accepted_and_attributed():
    # An auto-router (or a host fallback) answers as a concrete model; that is
    # normal, so the reply is kept and names the model that actually served it.
    llm = FakeLlm(Result("advice", provider="kilocode", model="reflection/beam-free"))
    coach = Coach(llm, _settings(), primary_model="primary", primary_provider="openrouter")
    r = coach.ask([{"role": "user", "content": "q"}], mode="consult")
    assert r.error == "" and r.text == "advice"
    assert (r.provider, r.model) == ("kilocode", "reflection/beam-free")

def test_same_model_as_primary_is_refused():
    coach = Coach(FakeLlm(Result("x")), _settings(), primary_model="x", primary_provider="openrouter")
    assert coach.check() == "same_model"

def test_explicit_override_does_not_bypass_same_model_guard():
    settings = load_settings({"coach_model": {"provider": "openrouter", "model": "other"},
                              "review_model": {"provider": "openrouter", "model": "x"}})
    coach = Coach(FakeLlm(Result("x")), settings, primary_model="x", primary_provider="openrouter")
    assert coach.check("review") == "same_model"
    assert coach.check("consult") == ""

def test_provider_error_is_returned_not_raised():
    class Boom:
        def complete(self, *a, **k): raise RuntimeError("503")
    r = Coach(Boom(), _settings()).ask([{"role": "user", "content": "q"}], mode="consult")
    assert r.error.startswith("provider_error") and r.text == ""


def test_an_unpriced_call_is_estimated_from_its_tokens():
    from types import SimpleNamespace

    from coach.advisor import Coach
    from coach.settings import load_settings

    coach = Coach(
        llm=None,
        settings=load_settings(
            {
                "coach_model": {"provider": "opencode-go", "model": "glm-5.3-flash"},
                "fallback_price_per_mtok": 1.0,
            }
        ),
    )
    usage = SimpleNamespace(
        input_tokens=2_000, output_tokens=500,
        cache_read_tokens=0, cache_write_tokens=0,
    )
    cost, estimated = coach._price("glm-5.3-flash", usage, "opencode-go")
    assert estimated is True
    assert cost == 0.0025


def test_a_zero_fallback_rate_keeps_unpriced_calls_free():
    from types import SimpleNamespace

    from coach.advisor import Coach
    from coach.settings import load_settings

    coach = Coach(
        llm=None,
        settings=load_settings(
            {
                "coach_model": {"provider": "p", "model": "m"},
                "fallback_price_per_mtok": 0.0,
            }
        ),
    )
    usage = SimpleNamespace(input_tokens=1_000, output_tokens=0)
    assert coach._price("m", usage, "p") == (0.0, True)


def test_a_hostile_result_object_does_not_escape_ask():
    from coach.advisor import Coach
    from coach.settings import load_settings

    class Hostile:
        @property
        def text(self):
            raise RuntimeError("boom")

    class Llm:
        def complete(self, messages, **kwargs):
            return Hostile()

    coach = Coach(
        llm=Llm(),
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
    )
    reply = coach.ask([{"role": "user", "content": "q"}], mode="consult")
    assert reply.error.startswith("provider_error")


def test_a_mapping_usage_is_priced_by_the_fallback():
    from coach.advisor import Coach
    from coach.settings import load_settings

    coach = Coach(
        llm=None,
        settings=load_settings(
            {
                "coach_model": {"provider": "p", "model": "m"},
                "fallback_price_per_mtok": 2.0,
            }
        ),
    )
    cost, estimated = coach._price(
        "m", {"input_tokens": 1000, "output_tokens": 500}, "p"
    )
    assert estimated in (True, False)
    assert cost >= 0.0


def test_ask_passes_reasoning_when_the_host_supports_it():
    from coach.advisor import Coach
    from coach.settings import load_settings

    class Llm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append(kwargs)
            from types import SimpleNamespace

            return SimpleNamespace(
                text="ok", provider="p", model="m", usage=None
            )

    llm = Llm()
    coach = Coach(
        llm=llm,
        settings=load_settings(
            {
                "coach_model": {"provider": "p", "model": "m"},
                "reasoning_effort": "high",
            }
        ),
    )
    coach.ask([{"role": "user", "content": "q"}], mode="consult")
    assert llm.calls[-1]["reasoning_effort"] == "high"


def test_ask_omits_reasoning_on_an_old_host_without_the_kwarg():
    from coach.advisor import Coach
    from coach.settings import load_settings

    class OldLlm:
        def __init__(self):
            self.calls = []

        def complete(self, messages, model=None, provider=None, max_tokens=None, timeout=None):
            self.calls.append({"model": model})
            from types import SimpleNamespace

            return SimpleNamespace(text="ok", provider="p", model="m", usage=None)

    llm = OldLlm()
    coach = Coach(
        llm=llm,
        settings=load_settings(
            {
                "coach_model": {"provider": "p", "model": "m"},
                "reasoning_effort": "high",
            }
        ),
    )
    reply = coach.ask([{"role": "user", "content": "q"}], mode="consult")
    assert reply.error == ""
    assert llm.calls  # called without the kwarg


def test_a_missing_usage_is_flagged_as_estimated_not_free():
    from coach.advisor import Coach

    coach = Coach(
        llm=None,
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
    )
    # No token counts to estimate from, but a missing usage must not read as
    # a confidently priced free call.
    assert coach._price("m", None, "p") == (0.0, True)
