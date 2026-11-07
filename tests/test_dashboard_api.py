# tests/test_dashboard_api.py
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from coach.findings import Finding
from coach.plugin import _primary_identity
from dashboard.plugin_api import router, app_for_tests
from tests.support import plugin


def test_status_reports_unconfigured(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    body = client.get("/status").json()
    assert body["configured"] is False
    assert body["permissions"]["model_overrides"] is False


def _remember(home, project):
    (home / "coach").mkdir(parents=True, exist_ok=True)
    (home / "coach" / "state.json").write_text(
        json.dumps({"seen_cwds": [str(project)]}), encoding="utf-8"
    )


def test_guidance_roundtrip(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    project = tmp_path / "proj"
    project.mkdir()
    _remember(tmp_path, project)
    r = client.put(
        "/guidance",
        json={"scope": "project", "cwd": str(project), "text": "no regex"},
    )
    assert r.status_code == 200
    assert (
        client.get(
            "/guidance", params={"scope": "project", "cwd": str(project)}
        ).json()["text"]
        == "no regex"
    )


def test_guidance_rejects_unregistered_path(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    r = client.put(
        "/guidance", json={"scope": "project", "cwd": "/etc", "text": "x"}
    )
    assert r.status_code == 400


def test_settings_save_roundtrip(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    r = client.put("/config", json={"values": {
        "active_enabled": True,
        "coach_model": {"provider": "p", "model": "m"},
    }})
    assert r.status_code == 200
    body = client.get("/config").json()
    assert body["values"]["active_enabled"] is True
    assert body["configured"] is True


def test_settings_reject_unknown_key(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    assert client.put("/config", json={"values": {"nope": 1}}).status_code == 400


def test_recent_returns_newest_first(tmp_path):
    logs = tmp_path / "coach" / "logs"
    logs.mkdir(parents=True)
    (logs / "a.jsonl").write_text(
        '{"kind":"review","at":"2026-01-01T00:00:00+00:00","title":"old","body":"b","severity":"nit"}\n'
        '{"kind":"review","at":"2026-02-01T00:00:00+00:00","title":"new","body":"b2","severity":"concern"}\n',
        encoding="utf-8",
    )
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    rows = client.get("/recent").json()["reviews"]
    assert [row["title"] for row in rows] == ["new", "old"]


def test_recent_can_filter_one_session(tmp_path):
    logs = tmp_path / "coach" / "logs"
    logs.mkdir(parents=True)
    (logs / "a.jsonl").write_text(
        '{"kind":"review","at":"2026-01-01T00:00:00+00:00","title":"one","session_id":"s1"}\n',
        encoding="utf-8",
    )
    (logs / "b.jsonl").write_text(
        '{"kind":"review","at":"2026-02-01T00:00:00+00:00","title":"two","session_id":"s2"}\n',
        encoding="utf-8",
    )
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    rows = client.get("/recent", params={"session": "s1"}).json()["reviews"]
    assert [row["title"] for row in rows] == ["one"]


def test_get_guidance_does_not_follow_symlinks(tmp_path):
    from dashboard.plugin_api import _read_guidance

    target = tmp_path / "real.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "COACH.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert _read_guidance(link, root=tmp_path) == ""


def test_get_guidance_rejects_paths_outside_the_project(tmp_path):
    from dashboard.plugin_api import _read_guidance

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    assert _read_guidance(outside, root=tmp_path / "project") == ""
    assert _read_guidance(outside, root=tmp_path) == "secret"


def test_recent_returns_only_the_tail_of_large_logs(tmp_path):
    logs = tmp_path / "coach" / "logs"
    logs.mkdir(parents=True)
    with (logs / "big.jsonl").open("w", encoding="utf-8") as handle:
        for index in range(200):
            handle.write(
                json.dumps(
                    {
                        "kind": "review",
                        "session_id": "s",
                        "turn_id": "t",
                        "kind": "review",
                        "severity": "nit",
                        "title": f"note {index}",
                        "at": f"2026-11-06T00:00:{index:02d}",
                    }
                )
                + "\n"
            )
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    payload = client.get("/recent", params={"limit": 5}).json()
    assert [row["title"] for row in payload["reviews"]] == [
        f"note {index}" for index in (199, 198, 197, 196, 195)
    ]


def test_project_scope_accepts_a_directory_the_plugin_has_seen(tmp_path):
    """The plugin records seen_cwds; without it project scope always refused."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "real-project"
    project.mkdir()
    (home / "coach").mkdir()
    (home / "coach" / "state.json").write_text(
        json.dumps({"seen_cwds": [str(project)]}), encoding="utf-8"
    )
    client = TestClient(app_for_tests(home=home), base_url="http://127.0.0.1")
    r = client.put(
        "/guidance", json={"scope": "project", "cwd": str(project), "text": "x"}
    )
    assert r.status_code == 200
    assert (project / "COACH.md").read_text() == "x"


def test_recent_excludes_decision_rows(tmp_path):
    from coach.plugin import _log_name

    logs = tmp_path / "coach" / "logs"
    logs.mkdir(parents=True)
    (logs / f"{_log_name('s')}.jsonl").write_text(
        '{"kind":"review","title":"kept","severity":"nit","at":"2026-01-01T00:00:01",'
        '"session_id":"s"}\n'
        '{"kind":"decide","decision":"stay","at":"2026-01-01T00:00:02",'
        '"session_id":"s"}\n',
        encoding="utf-8",
    )
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    rows = client.get("/recent").json()["reviews"]
    assert [row.get("title") for row in rows] == ["kept"]


def test_mutations_reject_a_foreign_origin(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    denied = client.post(
        "/permissions/grant", headers={"Origin": "https://evil.example"}
    )
    assert denied.status_code == 403
    allowed = client.post(
        "/permissions/grant", headers={"Origin": "http://localhost:3000"}
    )
    assert allowed.status_code == 200


def test_loopback_guard_accepts_only_exact_loopback_hosts(tmp_path):
    """Rebinding-class names and non-loopback hosts never pass the Host gate."""

    from dashboard.plugin_api import _is_loopback_host

    assert _is_loopback_host("localhost")
    assert _is_loopback_host("127.0.0.1")
    assert _is_loopback_host("::1")
    assert not _is_loopback_host("127.0.0.1.nip.io")
    assert not _is_loopback_host("127.gov")
    assert not _is_loopback_host("127.0.0.2")
    assert not _is_loopback_host(None)
    assert not _is_loopback_host("")


def test_mutations_reject_a_malformed_host_header():
    """A malformed Host refuses with the pinned 403, not an accidental 500."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from dashboard.plugin_api import _guard_origin

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/config",
            "query_string": b"",
            "headers": [(b"host", b"[::1")],
        }
    )
    with pytest.raises(HTTPException) as excinfo:
        _guard_origin(request)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "cross_origin_request"


# ---------------------------------------------------------------------------
# Dashboard API guards
# ---------------------------------------------------------------------------


def test_dashboard_config_rejects_secret_empty_and_malformed_values(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    assert (
        client.put(
            "/config", json={"values": {"decide_typesafe_api_key": "x"}}
        ).status_code
        == 400
    )
    assert client.put("/config", json={"values": {}}).status_code == 400
    assert client.put("/config", json={"values": ["x"]}).status_code == 400


def test_dashboard_grant_reports_permissions(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    assert client.post("/permissions/grant", json={}).status_code == 200
    config = client.get("/config").json()
    assert config["permissions"]["model_overrides"] is True


def test_dashboard_guidance_rejects_unknown_scope_and_oversize_text(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    assert (
        client.put(
            "/guidance",
            json={"scope": "everywhere", "cwd": "", "text": "x"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/guidance",
            json={"scope": "global", "cwd": "", "text": "x" * 8001},
        ).status_code
        == 400
    )


def test_dashboard_global_guidance_creates_its_parent(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    response = client.put(
        "/guidance", json={"scope": "global", "cwd": "", "text": "Be blunt."}
    )
    assert response.status_code == 200
    assert (tmp_path / "coach" / "guidance.md").read_text(
        encoding="utf-8"
    ) == "Be blunt."


# ---------------------------------------------------------------------------
# Dashboard endpoint behavior
# ---------------------------------------------------------------------------


def test_dashboard_recent_orders_and_filters(tmp_path):
    logs = tmp_path / "coach" / "logs"
    logs.mkdir(parents=True)
    (logs / "a.jsonl").write_text(
        '{"kind":"review","at":"2026-01-01T00:00:00+00:00","title":"old","body":"b",'
        '"severity":"nit","session_id":"s1"}\n',
        encoding="utf-8",
    )
    (logs / "b.jsonl").write_text(
        '{"kind":"review","at":"2026-02-01T00:00:00+00:00","title":"new","body":"b2",'
        '"severity":"concern","session_id":"s2"}\n',
        encoding="utf-8",
    )
    client = TestClient(app_for_tests(home=tmp_path), base_url="http://127.0.0.1")
    rows = client.get("/recent").json()["reviews"]
    assert [row["title"] for row in rows] == ["new", "old"]
    rows = client.get("/recent", params={"session": "s1"}).json()["reviews"]
    assert [row["title"] for row in rows] == ["old"]


# ---------------------------------------------------------------------------
# Grant, identity, and command model handling
# ---------------------------------------------------------------------------


def test_a_rebinding_host_is_refused(tmp_path):
    client = TestClient(
        app_for_tests(home=tmp_path), base_url="http://127.0.0.1"
    )
    # DNS rebinding: the page's own hostname reaches the loopback API.
    denied = client.post(
        "/permissions/grant",
        headers={"Host": "evil.example:8765", "Origin": "http://evil.example:8765"},
    )
    assert denied.status_code == 403


def test_a_loopback_host_with_a_foreign_origin_is_refused(tmp_path):
    client = TestClient(
        app_for_tests(home=tmp_path), base_url="http://127.0.0.1"
    )
    denied = client.put(
        "/guidance",
        json={"scope": "global", "text": "x"},
        headers={"Origin": "https://evil.example"},
    )
    assert denied.status_code == 403


def test_config_shows_the_effective_value_not_the_raw_one(tmp_path):
    app = app_for_tests(home=tmp_path)
    app.state.config = {
        "plugins": {
            "entries": {
                "coach": {"settings": {"active_every_n": 0, "budget_session_usd": 99999}}
            }
        }
    }
    body = TestClient(app, base_url="http://127.0.0.1").get("/config").json()
    assert body["values"]["active_every_n"] == 2
    assert body["values"]["budget_session_usd"] == 1000.0
