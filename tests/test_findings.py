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


def test_a_brace_inside_a_string_does_not_truncate_the_json():
    text = (
        "Here is the result:\n```json\n"
        '{"severity": "concern", "title": "Guard", "body": "uses } here", '
        '"evidence": "a.py:1", "fix": "Close it."}\n```\n'
    )
    finding = parse_finding(None, text)
    assert finding is not None
    assert finding.body == "uses } here"


def test_digest_separates_findings_with_different_advice():
    a = Finding("nit", "Same title", "one body", "a.py:1", "first fix")
    b = Finding("nit", "Same title", "two body", "a.py:1", "second fix")
    assert a.digest() != b.digest()
