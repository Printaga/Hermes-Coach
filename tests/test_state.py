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
    assert len(list(tmp_path.iterdir())) == 1
    assert store.sessions_on_disk() == ["s1"]

def test_prune_keeps_newest_twenty(tmp_path):
    store = Store(tmp_path, max_sessions=20)
    for i in range(25):
        s = store.load(f"s{i:02d}"); s.updated_at = float(i); store.save(s)
    store.prune()
    assert len(store.sessions_on_disk()) == 20
    assert "s00" not in store.sessions_on_disk()

def test_corrupt_file_is_reset_not_raised(tmp_path):
    store = Store(tmp_path)
    store._path("bad").write_text("{nope")
    s = store.load("bad")
    assert s.ledger == [] and s.last_error
