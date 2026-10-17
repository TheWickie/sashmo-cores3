import csv
import io
import wave

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


def test_silent_follow_up_returns_no_audio(monkeypatch):
    import sashmo.api as api_module
    options = []

    async def silent_audio(*args, **kwargs):
        options.append(kwargs.get("silent_on_no_speech"))
        return "", "", b""

    monkeypatch.setattr(api_module, "process_audio", silent_audio)
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\0\0" * 16000)
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False)
    with TestClient(app) as client:
        response = client.post("/v1/voice", headers={"X-Sashmo-Token": "x" * 32,
                                                    "X-Sashmo-Follow-Up": "1"},
                               content=payload.getvalue())
    assert response.status_code == 204
    assert response.content == b""
    assert options == [True]


def test_voice_volume_command_returns_device_setting_header(monkeypatch, tmp_path):
    import sashmo.speech as speech_module
    from sashmo.conversation import ConversationProvider

    async def spoken_volume(_audio):
        return "Set volume to 30 percent", 1.0

    async def spoken_reply(_answer):
        return b"audio"

    monkeypatch.setattr(speech_module, "transcribe", spoken_volume)
    monkeypatch.setattr(speech_module, "synthesize_reply", spoken_reply)
    provider = ConversationProvider(tmp_path / "projects.local.json")
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\0\0" * 16000)
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False,
                     conversation=provider)
    with TestClient(app) as client:
        response = client.post("/v1/voice", headers={"X-Sashmo-Token": "x" * 32},
                               content=payload.getvalue())
    assert response.status_code == 200
    assert response.headers["X-Sashmo-Volume-Percent"] == "30"


def test_wake_audio_is_authenticated_bounded_and_local(monkeypatch):
    import sashmo.api as api_module
    recognized = []
    monkeypatch.setattr(api_module, "recognize_wake", lambda audio: recognized.append(len(audio)) or True)
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\0\0" * 16000)
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False)
    with TestClient(app, client=("192.168.1.10", 50000)) as client:
        assert client.post("/v1/wake", content=payload.getvalue()).status_code == 401
        headers = {"X-Sashmo-Token": "x" * 32}
        assert client.post("/v1/wake", headers=headers, content=b"bad").status_code == 400
        response = client.post("/v1/wake", headers=headers, content=payload.getvalue())
        assert response.json() == {"wake": True}
        assert recognized == [len(payload.getvalue())]
        voice = client.get("/v1/status", headers=headers).json()["voice"]
        assert voice["wake_checks"] == 1
        assert voice["wake_detections"] == 1


def test_status_reports_authenticated_device_telemetry_without_changing_quota_version():
    state = StateStore()
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False, state=state)
    with TestClient(app, client=("192.168.1.10", 50000)) as client:
        assert client.get("/v1/status").status_code == 401
        headers = {"X-Sashmo-Token": "x" * 32}
        pulse = client.get("/v1/pulse?since=-1&battery=42&charging=1&usb=1&screen_sleeping=1&wifi_rssi=-51", headers=headers)
        assert pulse.status_code == 200
        assert pulse.json()["version"] == 0
        status = client.get("/v1/status", headers=headers).json()
        assert status["device"]["connected_recently"] is True
        assert status["device"]["battery_percent"] == 42
        assert status["device"]["charging"] is True
        assert status["device"]["usb_powered"] is True
        assert status["device"]["wifi_rssi"] == -51
        assert status["device"]["screen_sleeping"] is True


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
    with TestClient(app, client=("192.168.1.10", 50000)) as client:
        assert client.get("/v1/projects").status_code == 401
        response = client.get("/v1/projects", headers={"X-Sashmo-Token": "x" * 32})
        assert response.status_code == 200
        assert set(response.json()) == {"projects", "error"}


def test_personal_dashboard_endpoints_reject_authenticated_lan_device():
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False)
    with TestClient(app, client=("192.168.1.44", 50000)) as client:
        headers = {"X-Sashmo-Token": "x" * 32}
        assert client.get("/v1/pulse", headers=headers).status_code == 200
        assert client.get("/v1/status", headers=headers).status_code == 403
        assert client.get("/v1/projects", headers=headers).status_code == 403
        assert client.post("/v1/codex-task/approve", headers=headers).status_code == 403
        assert client.post("/v1/codex-task/reject", headers=headers).status_code == 403


def test_codex_task_cannot_launch_without_confirmed_voice_draft():
    app = create_app(Config("192.168.1.10", 8765, "x" * 32), start_worker=False)
    with TestClient(app, client=("192.168.1.10", 50000)) as client:
        headers = {"X-Sashmo-Token": "x" * 32}
        assert client.post("/v1/codex-task/approve", headers=headers).status_code == 409


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
