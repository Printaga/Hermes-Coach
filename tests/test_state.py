# tests/test_state.py
import os
import sys

import pytest

from coach.state import Store

_not_readable_as_non_owner = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 is not unreadable for root or on Windows",
)

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


def test_save_sweeps_old_sessions(tmp_path):
    store = Store(tmp_path, max_sessions=2)
    for index in range(3):
        store.save(store.load(f"s{index}"))
    for _ in range(22):
        store.save(store.load("s2"))
    assert len(store.sessions_on_disk()) <= 2


def test_has_reports_a_known_session(tmp_path):
    store = Store(tmp_path)
    assert store.has("s1") is False
    store.save(store.load("s1"))
    assert store.has("s1") is True


@_not_readable_as_non_owner
def test_unreadable_file_names_the_cause_in_last_error(tmp_path):
    store = Store(tmp_path)
    path = store._path("denied")
    path.write_text('{"session_id": "denied"}', encoding="utf-8")
    path.chmod(0o000)
    try:
        state = store.load("denied")
    finally:
        path.chmod(0o600)
    assert state.session_id == "denied"
    assert "PermissionError" in state.last_error
    assert state.last_error != "state reset"


def test_corrupt_files_are_skipped_by_listings_not_surfaced_as_ids(tmp_path):
    store = Store(tmp_path)
    good = store.load("s1")
    store.save(good)
    corrupt = store._path("bad")
    corrupt.write_text("{nope", encoding="utf-8")
    assert store.sessions_on_disk() == ["s1"]
    assert store.sessions_by_recency() == ["s1"]
    assert store.spend_total() == 0.0


def test_missing_session_id_is_skipped_by_listings(tmp_path):
    store = Store(tmp_path)
    store.save(store.load("s1"))
    store._path("anon").write_text('{"ledger": []}', encoding="utf-8")
    assert store.sessions_on_disk() == ["s1"]


def test_prune_sweeps_stale_tmp_files(tmp_path):
    store = Store(tmp_path)
    store.save(store.load("s1"))
    stale = tmp_path / ".s1-abc123.tmp"
    stale.write_text("partial", encoding="utf-8")
    old = 7200.0
    import os as _os

    _os.utime(stale, (old, old))
    fresh = tmp_path / ".s1-def456.tmp"
    fresh.write_text("partial", encoding="utf-8")
    store.prune()
    assert not stale.exists()
    assert fresh.exists()


def test_a_huge_integer_in_a_session_file_does_not_raise(tmp_path):
    store = Store(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / store._path("s").name).write_text(
        '{"session_id": "s", "spend_usd": ' + "9" * 400 + "}",
        encoding="utf-8",
    )
    state = store.load("s")  # "never raises" is the contract
    assert state.spend_usd == 0.0
    assert state.session_id == "s"


def test_corrupt_file_is_quarantined_for_diagnosis(tmp_path):
    store = Store(tmp_path)
    corrupt = store._path("bad")
    corrupt.write_text("{nope", encoding="utf-8")
    s = store.load("bad")
    assert s.ledger == [] and s.last_error
    # The unreadable file survives under a quarantined name,...
    quarantined = list(tmp_path.glob("*.json.corrupt-*"))
    assert len(quarantined) == 1
    # ...must not poison later listings, and the next save makes a normal file.
    assert store.sessions_on_disk() == []
    store.save(store.load("bad"))
    assert store.sessions_on_disk() == ["bad"]
