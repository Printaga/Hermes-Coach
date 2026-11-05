# tests/test_plugin_delivery.py
import json
from coach.findings import Finding
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

def test_corrective_injects_immediately_and_queues_nothing(tmp_path):
    p = _p(tmp_path)
    p.deliver(Finding("blocker", "Data loss", "Deletes before copy.",
                      "store.py:40", "Copy first."), session_id="s", turn_id="t1")
    assert p._test_inject and "Data loss" in p._test_inject[0]
    state = p.store.load("s")
    assert state.pending == [] and state.holds == 1

def test_corrective_cap_falls_back_to_ambient(tmp_path):
    p = _p(tmp_path, delivery_max_corrective_per_request=1)
    st = p.store.load("s"); st.holds = 1; p.store.save(st)
    p.deliver(Finding("blocker", "Data loss", "Deletes before copy.",
                      "store.py:40", "Copy first."), session_id="s", turn_id="t1")
    assert p._test_inject == []
    assert p.store.load("s").pending

def test_show_cards_posts_an_ambient_card(tmp_path):
    p = _p(tmp_path)
    p.deliver(Finding("nit", "Spacing", "One space.", "a.py:1", "Drop it."),
              session_id="s", turn_id="t1")
    assert p._test_post and "Spacing" in p._test_post[0]

def test_expired_note_is_dropped(tmp_path):
    p = _p(tmp_path, delivery_note_ttl_turns=0)
    p.deliver(Finding("nit", "Spacing", "One space.", "a.py:1", "Drop it."),
              session_id="s", turn_id="t1")
    assert p.on_pre_llm_call(session_id="s", turn_id="t2",
                             conversation_history=[]) is None
    assert p.store.load("s").pending == []
