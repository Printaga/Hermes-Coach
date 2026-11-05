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
