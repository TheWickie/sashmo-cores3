import csv

from fastapi.testclient import TestClient

from sashmo.api import Config, create_app
from sashmo.state import StateStore


def test_authenticated_versioned_pulse():
    state = StateStore()
    state.update({"plan_reported": "example-plan", "buckets": []}, True, None)
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False, state=state)
    with TestClient(app) as client:
        assert client.get("/v1/pulse").status_code == 401
        headers = {"X-Sashmo-Token": "x" * 32}
        changed = client.get("/v1/pulse?since=-1", headers=headers).json()
        assert changed["quota"]["plan_reported"] == "example-plan"
        same = client.get(f"/v1/pulse?since={changed['version']}", headers=headers).json()
        assert set(same) == {"version", "time", "date"}


def test_voice_rejects_non_wav():
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False)
    with TestClient(app) as client:
        response = client.post("/v1/voice", headers={"X-Sashmo-Token": "x" * 32}, content=b"not audio")
        assert response.status_code == 400


def test_status_reports_authenticated_device_telemetry_without_changing_quota_version():
    state = StateStore()
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False, state=state)
    with TestClient(app) as client:
        assert client.get("/v1/status").status_code == 401
        headers = {"X-Sashmo-Token": "x" * 32}
        pulse = client.get("/v1/pulse?since=-1&battery=42&charging=1&usb=1&wifi_rssi=-51", headers=headers)
        assert pulse.status_code == 200
        assert pulse.json()["version"] == 0
        status = client.get("/v1/status", headers=headers).json()
        assert status["device"]["connected_recently"] is True
        assert status["device"]["battery_percent"] == 42
        assert status["device"]["charging"] is True
        assert status["device"]["usb_powered"] is True
        assert status["device"]["wifi_rssi"] == -51


def test_battery_history_records_authenticated_pulses_only(tmp_path):
    history = tmp_path / "battery-history.csv"
    state = StateStore(telemetry_path=history)
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False, state=state)
    with TestClient(app) as client:
        assert client.get("/v1/pulse?battery=70").status_code == 401
        assert not history.exists()
        headers = {"X-Sashmo-Token": "x" * 32}
        assert client.get("/v1/pulse?battery=70&charging=0&usb=0&wifi_rssi=-64",
                          headers=headers).status_code == 200
        assert client.get("/v1/pulse?battery=69&charging=0&usb=0&wifi_rssi=-65",
                          headers=headers).status_code == 200
    with history.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert [row["battery_percent"] for row in rows] == ["70", "69"]
    assert all(row["usb_powered"] == "False" for row in rows)
    assert all(set(row) == {"observed_at_utc", "battery_percent", "charging",
                            "usb_powered", "wifi_rssi"} for row in rows)


def test_projects_endpoint_requires_device_token():
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False)
    with TestClient(app) as client:
        assert client.get("/v1/projects").status_code == 401
        response = client.get("/v1/projects", headers={"X-Sashmo-Token": "x" * 32})
        assert response.status_code == 200
        assert set(response.json()) == {"projects", "error"}


def test_low_battery_notification_is_deduplicated_and_cleared():
    state = StateStore()
    state.record_device(14, False, False, -55)
    first_version = state.version
    assert state.pulse(-1)["notification_count"] == 1
    assert state.pulse(-1)["notifications"][0]["title"] == "Low battery"
    state.record_device(14, False, False, -55)
    assert state.version == first_version
    state.record_device(16, True, True, -55)
    assert state.status()["notifications"]["count"] == 0
    assert state.version == first_version + 1
