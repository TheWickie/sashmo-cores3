"""The iPad can see only a paired, read-only projection of desktop state."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from sashmo.api import Config, create_app
from sashmo.ipad import IpadNotificationStore, changes, project_sections
from sashmo.state import StateStore


CONFIG = Config("192.168.1.10", 8766, "x" * 32,
                ipad_featured_projects=("Example project",))


def _client(app, host="192.168.1.44"):
    return TestClient(app, client=(host, 50000))


def _pair(client, app):
    code = app.state.ipad_pairing.current_code()["code"]
    response = client.post("/ipad/api/pair", json={"code": code},
                           headers={"Origin": "http://192.168.1.10:8766"})
    assert response.status_code == 200
    return response


def test_shell_manifest_and_existing_private_api():
    app = create_app(CONFIG, start_worker=False)
    with _client(app) as client:
        for path in ("/", "/ipad", "/ipad/"):
            page = client.get(path)
            assert page.status_code == 200
            assert "Sashmo Command Centre" in page.text
            assert CONFIG.device_token not in page.text
        assert client.get("/docs").status_code == 404
        assert client.get("/ipad/manifest.webmanifest").json()["display"] == "standalone"
        assert client.get("/ipad/assets/app.js").status_code == 200
        assert client.get("/ipad/sw.js").status_code == 200
        assert client.get("/ipad/api/bootstrap").json() == {"paired": False}
        assert client.get("/ipad/api/updates").status_code == 401
        assert client.get("/v1/status").status_code == 401
        assert client.get("/v1/projects", headers={"X-Sashmo-Token": CONFIG.device_token}).status_code == 403


def test_pairing_home_network_origin_cookie_and_rate_limit():
    app = create_app(CONFIG, start_worker=False)
    with _client(app, "192.168.2.5") as outsider:
        assert outsider.get("/ipad/api/bootstrap").status_code == 403
        assert outsider.post("/ipad/api/pair", json={"code": "00000000"}).status_code == 403
    with _client(app) as client:
        code = app.state.ipad_pairing.current_code()["code"]
        assert client.post("/ipad/api/pair", json={"code": code},
                           headers={"Origin": "http://evil.invalid"}).status_code == 403
        response = _pair(client, app)
        assert "httponly" in response.headers["set-cookie"].lower()
        assert "samesite=strict" in response.headers["set-cookie"].lower()
        assert client.get("/ipad/api/bootstrap").json() == {"paired": True}
        assert client.post("/v1/codex-task/approve").status_code == 401
    with _client(app, "192.168.1.45") as failed:
        for _ in range(5):
            assert failed.post("/ipad/api/pair", json={"code": "11111111"}).status_code == 429
        assert failed.post("/ipad/api/pair", json={"code": code}).status_code == 429


def test_reported_quota_changes_and_notification_history():
    state = StateStore()
    state.update({"plan_reported": "test-plan", "buckets": [{"id": "codex", "windows": [
        {"slot": "primary", "remaining_percent": 73, "duration_mins": 192,
         "resets_at_local": "2026-10-24T21:00:00+01:00"}]}]}, True, None)
    state.update_codex_sessions([{"label": "Codex task 1", "state": "completed"}], None)
    state.notify("example_alert", "A genuine alert", "Review on Windows")
    app = create_app(CONFIG, start_worker=False, state=state)
    with _client(app) as client:
        _pair(client, app)
        first = client.get("/ipad/api/updates")
        assert first.status_code == 200
        payload = first.json()
        assert payload["sections"]["codex"]["buckets"][0]["windows"][0]["duration_mins"] == 192
        assert payload["sections"]["codex"]["completed_sessions"][0]["state"] == "completed"
        assert payload["sections"]["development"]["named_projects"][0]["name"] == "Example project"
        assert payload["sections"]["chatgpt"]["reminders"] == []
        assert payload["sections"]["communications"]["gmail"]["connected"] is False
        item = payload["sections"]["notifications"]["items"][0]
        assert item["title"] == "A genuine alert" and item["priority"] == "high"
        assert item["read"] is False and item["timestamp"]
        versions = payload["versions"]
        assert client.get("/ipad/api/updates", params={"v_" + k: v for k, v in versions.items()}).status_code == 204
        assert client.post(f"/ipad/api/notifications/{item['id']}/read", json={"read": True},
                           headers={"Origin": "http://192.168.1.10:8766"}).status_code == 200
        delta = client.get("/ipad/api/updates", params={"v_" + k: v for k, v in versions.items()}).json()
        assert set(delta["sections"]) == {"notifications"}
        assert delta["sections"]["notifications"]["items"][0]["read"] is True


def test_preferences_and_invalid_cookie_are_bounded():
    app = create_app(CONFIG, start_worker=False)
    with _client(app) as client:
        _pair(client, app)
        assert client.post("/ipad/api/preferences", json={"minimum_priority": "high"},
                           headers={"Origin": "http://evil.invalid"}).status_code == 403
        assert client.post("/ipad/api/preferences", json={"minimum_priority": []}).status_code == 400
        assert client.post("/ipad/api/preferences", json={"night_start": "99:99"}).status_code == 400
        saved = client.post("/ipad/api/preferences", json={"minimum_priority": "high"}).json()
        assert saved["preferences"]["minimum_priority"] == "high"
        client.cookies.set("sashmo_ipad", "%%%", path="/ipad")
        assert client.get("/ipad/api/updates").status_code == 401


def test_projection_keeps_unavailable_sources_explicit_and_does_not_invent_windows():
    state = StateStore()
    state.update({"buckets": []}, False, "Codex unavailable")
    state.chatgpt_checked_at = datetime.now(timezone.utc)
    state.chatgpt_error = "Sign-in required"
    result = project_sections(state, IpadNotificationStore(), {"projects": [], "error": None})
    assert result["codex"]["buckets"] == []
    assert result["chatgpt"]["reminders"] == []
    assert result["chatgpt"]["reminders_error"] == "Sign-in required"
    assert result["communications"]["calendar"]["connected"] is False
    assert changes(result, {name: "" for name in result})["sections"] == result


def test_gmail_important_label_becomes_high_priority_without_mail_body():
    state = StateStore()
    state.update_gmail({"unread_count": 1, "messages": [{"id": "m1", "subject": "Check this",
                        "from": "sender@example.test", "unread": True, "important": True}]}, None)
    notifications = IpadNotificationStore()
    notifications.ingest(state.notification_feed())
    result = project_sections(state, notifications)
    assert result["notifications"]["items"][0]["priority"] == "high"
    assert result["communications"]["gmail"]["messages"][0]["important"] is True
    assert "body" not in str(result["communications"]).lower()
