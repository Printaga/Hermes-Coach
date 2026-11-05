# tests/test_delivery.py
from coach.delivery import format_ambient, format_card, format_corrective, route
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

def test_corrective_policy_covers_non_blockers_on_cli():
    s = load_settings({"delivery_policy": "corrective"})
    nit = Finding("nit", "Spacing", "One space.", "a.py:1", "Drop it.")
    assert route(nit, s, surface="cli", holds_used=0) == "corrective"

def test_non_deliverable_finding_is_dropped():
    s = load_settings({})
    assert route(Finding("none", "", "", "", ""), s, surface="cli", holds_used=0) == "drop"

def test_card_and_corrective_keep_the_finding_attribution():
    card = format_card(F)
    corrective = format_corrective(F)
    assert "blocker" in card and "Data loss" in card and "store.py:40" in card
    assert "Data loss" in corrective and "store.py:40" in corrective
