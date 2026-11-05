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
