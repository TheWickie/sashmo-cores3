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
