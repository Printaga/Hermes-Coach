# tests/test_review_gate.py
from coach.findings import Finding
from coach.plugin import CoachPlugin
from coach.settings import load_settings
from coach.state import Store
from tests.test_plugin_ask import Llm


class StructuredLlm:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        payload = self.payload

        class R:
            text = ""
            usage = None
            parsed = None

        result = R()
        result.parsed = payload
        result.model = kw["model"]
        result.provider = kw["provider"]
        return result


def _p(tmp_path, *, llm=None, **cfg):
    base = {"coach_model": {"provider": "p", "model": "m"}}
    base.update(cfg)
    posted = []
    p = CoachPlugin(
        llm=llm or Llm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings(base),
        post=posted.append,
    )
    p._test_post = posted
    return p


PAY = dict(
    session_id="s",
    turn_id="t1",
    final_response="All done.",
    changed_paths=["a.py"],
    model="primary",
    coding=True,
    attempt=0,
)

FINDING = {
    "severity": "blocker",
    "title": "Deletes before copying",
    "body": "The copy happens after the delete.",
    "evidence": "store.py:40",
    "fix": "Copy first.",
}


def test_review_skipped_when_disabled(tmp_path):
    p = _p(tmp_path, review_enabled=False)
    assert p.on_pre_verify(**PAY) is None


def test_report_authority_never_holds(tmp_path):
    p = _p(tmp_path, review_authority="report", llm=StructuredLlm(FINDING))
    p.collect_evidence = lambda cwd, paths: "diff --git a/a.py"
    out = p.on_pre_verify(**PAY)
    assert out is None or "action" not in out
    assert p._test_post


def test_hold_is_capped_per_request(tmp_path):
    p = _p(tmp_path, review_authority="hold_once")
    p.collect_evidence = lambda cwd, paths: "diff --git a/a.py"
    p.review_now = lambda **kw: type(
        "F",
        (),
        {
            "severity": "blocker",
            "title": "t",
            "body": "b",
            "evidence": "e",
            "fix": "f",
        },
    )()
    first = p.on_pre_verify(**PAY)
    second = p.on_pre_verify(**PAY)
    assert first["action"] == "continue"
    assert second is None


def test_no_changed_paths_skips_the_call(tmp_path):
    p = _p(tmp_path)

    def fail(**kw):
        raise AssertionError("review_now should not run")

    p.review_now = fail
    assert p.on_pre_verify(**dict(PAY, changed_paths=[])) is None


def test_budget_soft_stop_skips_review(tmp_path):
    p = _p(tmp_path, budget_session_usd=0.01, budget_on_exceed="soft")
    state = p.store.load("s")
    state.spend_usd = 1.0
    p.store.save(state)
    called = []
    p.review_now = lambda **kw: called.append(kw) or None
    assert p.on_pre_verify(**PAY) is None
    assert called == []


def test_review_now_uses_a_twenty_second_deadline(tmp_path):
    llm = StructuredLlm(FINDING)
    p = _p(tmp_path, llm=llm)
    finding = p.review_now(
        session_id="s",
        turn_id="t1",
        request="Fix the store.",
        claim="All done.",
        evidence="diff --git a/store.py",
    )
    assert finding is not None and finding.severity == "blocker"
    assert llm.calls and llm.calls[0]["timeout"] == 20.0


def test_non_blocker_is_reported_as_a_card(tmp_path):
    p = _p(tmp_path)
    p.collect_evidence = lambda cwd, paths: "diff --git a/a.py"
    p.review_now = lambda **kw: Finding(
        "concern", "Missing check", "No validation.", "a.py:1", "Validate."
    )
    assert p.on_pre_verify(**PAY) is None
    assert p._test_post and "Missing check" in p._test_post[0]


def test_evidence_uses_the_session_cwd(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    p = CoachPlugin(
        llm=Llm(),
        store=Store(tmp_path / "sessions"),
        settings=load_settings({"coach_model": {"provider": "p", "model": "m"}}),
        cwd=lambda: str(project),
    )
    seen = {}
    p.collect_evidence = lambda cwd, paths: seen.setdefault("cwd", cwd) or ""
    p.review_now = lambda **kw: None
    p.on_pre_verify(**PAY)
    assert seen["cwd"] == str(project)


def test_review_now_logs_one_line(tmp_path):
    p = _p(tmp_path, llm=StructuredLlm(FINDING))
    p.review_now(session_id="s", turn_id="t1", request="r", claim="c", evidence="e")
    lines = (tmp_path / "logs" / "s.jsonl").read_text().splitlines()
    assert len(lines) == 1 and '"severity": "blocker"' in lines[0]
