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
    project = tmp_path / "proj"
    project.mkdir()
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
    client = TestClient(app_for_tests(home=tmp_path))
    r = client.put(
        "/guidance", json={"scope": "project", "cwd": "/etc", "text": "x"}
    )
    assert r.status_code == 400


def test_settings_save_roundtrip(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    r = client.put("/config", json={"values": {
        "active_enabled": True,
        "coach_model": {"provider": "p", "model": "m"},
    }})
    assert r.status_code == 200
    body = client.get("/config").json()
    assert body["values"]["active_enabled"] is True
    assert body["configured"] is True


def test_settings_reject_unknown_key(tmp_path):
    client = TestClient(app_for_tests(home=tmp_path))
    assert client.put("/config", json={"values": {"nope": 1}}).status_code == 400


def test_recent_returns_newest_first(tmp_path):
    logs = tmp_path / "coach" / "logs"
    logs.mkdir(parents=True)
    (logs / "a.jsonl").write_text(
        '{"at":"2026-01-01T00:00:00+00:00","title":"old","body":"b","severity":"nit"}\n'
        '{"at":"2026-02-01T00:00:00+00:00","title":"new","body":"b2","severity":"concern"}\n',
        encoding="utf-8",
    )
    client = TestClient(app_for_tests(home=tmp_path))
    rows = client.get("/recent").json()["reviews"]
    assert [row["title"] for row in rows] == ["new", "old"]


def test_recent_can_filter_one_session(tmp_path):
    logs = tmp_path / "coach" / "logs"
    logs.mkdir(parents=True)
    (logs / "a.jsonl").write_text(
        '{"at":"2026-01-01T00:00:00+00:00","title":"one","session_id":"s1"}\n',
        encoding="utf-8",
    )
    (logs / "b.jsonl").write_text(
        '{"at":"2026-02-01T00:00:00+00:00","title":"two","session_id":"s2"}\n',
        encoding="utf-8",
    )
    client = TestClient(app_for_tests(home=tmp_path))
    rows = client.get("/recent", params={"session": "s1"}).json()["reviews"]
    assert [row["title"] for row in rows] == ["one"]
