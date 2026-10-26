"""Deck command safety and the desktop-only status projection."""

from datetime import datetime, timezone
from fastapi.testclient import TestClient

from sashmo.api import Config, create_app
from sashmo.deck_commands import DeckCommands
from sashmo.state import StateStore


def test_command_ack_expiry_and_dedup():
    now = [10.0]
    commands = DeckCommands(lambda: now[0])
    issued = commands.issue_talk(True)
    first = commands.for_pulse()
    assert first["kind"] == "talk" and first["seq"] == issued["seq"]
    assert commands.for_pulse()["seq"] == first["seq"]
    assert commands.for_pulse(first["seq"], "started") is None
    assert commands.status()["state"] == "started"
    commands.issue_talk(True)
    now[0] = 101.0
    assert commands.for_pulse() is None
    assert commands.status()["state"] == "expired"
    commands.note_client_poll()
    assert commands.fast_poll_enabled()
    now[0] += 21
    assert not commands.fast_poll_enabled()


def test_local_deck_status_and_talk_are_not_lan_controls():
    state = StateStore()
    state.device_last_seen = datetime.now(timezone.utc)
    state.update_deck_vscode([{"thread_id": "thread1", "title": "Fix a task", "source": "vscode"}], None)
    state.notify("sample", "Review needed", "Check on Windows")
    config = Config("192.168.1.10", 8766, "x" * 32)
    app = create_app(config, start_worker=False, state=state)
    headers = {"X-Sashmo-Token": config.device_token}
    with TestClient(app, client=("192.168.1.44", 50000)) as remote:
        assert remote.get("/v1/deck/status", headers=headers).status_code == 403
        assert remote.post("/v1/deck/talk", headers=headers).status_code == 403
        assert remote.post("/v1/deck/notifications/" + "a"*24 + "/read",
                           json={"read": True}, headers=headers).status_code == 403
    with TestClient(app, client=("192.168.1.10", 50000)) as local:
        assert local.get("/v1/deck/status").status_code == 401
        snapshot = local.get("/v1/deck/status", headers=headers).json()
        assert snapshot["vscode"]["items"][0]["thread_id"] == "thread1"
        assert snapshot["deck_unread_count"] == 1
        item_id = snapshot["deck_notifications"][0]["id"]
        assert local.post(f"/v1/deck/notifications/{item_id}/read", json={"read": True},
                          headers=headers).json() == {"ok": True}
        assert local.get("/v1/deck/status", headers=headers).json()["deck_notifications"][0]["read"] is True
        assert local.get("/v1/deck/status", headers=headers).json()["deck_unread_count"] == 0
        assert local.get("/v1/pulse", headers=headers).json()["deck_remote_enabled"] is True
        issued = local.post("/v1/deck/talk", headers=headers)
        assert issued.status_code == 200
        assert local.post("/v1/deck/talk", headers=headers).status_code == 409
        pulse = local.get("/v1/pulse?since=0", headers=headers).json()
        assert pulse["deck_command"]["seq"] == issued.json()["seq"]
        ack = local.get(f"/v1/pulse?command_ack={issued.json()['seq']}&command_result=started", headers=headers)
        assert ack.json()["deck_command"] is None
        assert local.get("/v1/deck/status", headers=headers).json()["deck_talk"]["state"] == "started"
