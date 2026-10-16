"""Versioned device state and an independently refreshed Codex quota cache."""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex_rpc import CodexAppServer
from .codex_sessions import recent_sessions
from .quota import normalize_quota


class StateStore:
    def __init__(self, telemetry_path: Path | None = None) -> None:
        self.version = 0
        self.quota: dict[str, Any] | None = None
        self.codex_connected = False
        self.error: str | None = "Waiting for Codex"
        self.last_updated_local: str | None = None
        self.started_at = datetime.now(timezone.utc)
        self.device_last_seen: datetime | None = None
        self.device_battery_percent: int | None = None
        self.device_charging: bool | None = None
        self.device_usb_powered: bool | None = None
        self.device_wifi_rssi: int | None = None
        self.voice_state = "idle"
        self.voice_provider = "local"
        self.last_voice_local: str | None = None
        self.notifications: list[dict[str, str]] = []
        self.gmail_snapshot: dict[str, Any] | None = None
        self.gmail_error: str | None = "Not connected"
        self.calendar_snapshot: dict[str, Any] | None = None
        self.calendar_error: str | None = "Not connected"
        self.codex_sessions: list[dict[str, Any]] = []
        self.codex_sessions_error: str | None = "Waiting for Codex"
        self.telemetry_path = telemetry_path

    def update(self, quota: dict[str, Any] | None, connected: bool, error: str | None) -> None:
        if (quota, connected, error) != (self.quota, self.codex_connected, self.error):
            self.version += 1
            self.quota = quota
            self.codex_connected = connected
            self.error = error
            if connected:
                self.last_updated_local = datetime.now().astimezone().isoformat(timespec="seconds")

    def pulse(self, since: int) -> dict[str, Any]:
        now = datetime.now().astimezone()
        basic = {"version": self.version, "time": now.strftime("%H:%M"), "date": now.strftime("%d %b %Y")}
        if since == self.version:
            return basic
        return {
            **basic,
            "quota": self.quota,
            "codex_connected": self.codex_connected,
            "error": self.error,
            "last_updated_local": self.last_updated_local,
            "notification_count": len(self.notifications),
            "notifications": self.notifications[-3:],
            "gmail_unread_count": ((self.gmail_snapshot or {}).get("unread_count")
                                   if self.gmail_snapshot is not None and self.gmail_error is None else None),
            "calendar_event_count": (len((self.calendar_snapshot or {}).get("events") or [])
                                     if self.calendar_snapshot is not None and self.calendar_error is None else None),
        }

    def update_gmail(self, snapshot: dict[str, Any] | None, error: str | None) -> None:
        previous_count = (self.gmail_snapshot or {}).get("unread_count")
        previous_error = self.gmail_error
        new_count = (snapshot or {}).get("unread_count")
        if snapshot is not None:
            self.gmail_snapshot = snapshot
        elif error == "Not connected":
            self.gmail_snapshot = None
            new_count = None
        self.gmail_error = error
        if new_count != previous_count or error != previous_error:
            self.version += 1

    def update_calendar(self, snapshot: dict[str, Any] | None, error: str | None) -> None:
        previous = (self.calendar_snapshot or {}).get("events")
        previous_error = self.calendar_error
        if snapshot is not None:
            self.calendar_snapshot = snapshot
        elif error == "Not connected":
            self.calendar_snapshot = None
        self.calendar_error = error
        current = (self.calendar_snapshot or {}).get("events")
        if current != previous or error != previous_error:
            self.version += 1

    def update_codex_sessions(self, sessions: list[dict[str, Any]] | None,
                              error: str | None) -> None:
        if sessions is not None:
            self.codex_sessions = sessions
        self.codex_sessions_error = error

    def notify(self, key: str, title: str, summary: str) -> None:
        """Replace one local alert by stable key; do not include credentials or transcripts."""
        item = {"key": key[:64], "title": title[:60], "summary": summary[:120]}
        if any(existing == item for existing in self.notifications):
            return
        self.notifications = [entry for entry in self.notifications if entry["key"] != item["key"]]
        self.notifications.append(item)
        self.notifications = self.notifications[-10:]
        self.version += 1

    def clear_notification(self, key: str) -> None:
        remaining = [entry for entry in self.notifications if entry["key"] != key]
        if len(remaining) != len(self.notifications):
            self.notifications = remaining
            self.version += 1

    def record_device(self, battery: int | None, charging: bool | None,
                      usb: bool | None, wifi_rssi: int | None) -> None:
        self.device_last_seen = datetime.now(timezone.utc)
        self.device_battery_percent = battery if battery is not None and 0 <= battery <= 100 else None
        self.device_charging = charging
        self.device_usb_powered = usb
        self.device_wifi_rssi = wifi_rssi if wifi_rssi is not None and -120 <= wifi_rssi <= 0 else None
        if usb is False and battery is not None and battery <= 15:
            self.notify("battery_low", "Low battery", f"CoreS3 battery is at {battery}%.")
        elif usb is True or (battery is not None and battery > 20):
            self.clear_notification("battery_low")
        if self.telemetry_path is not None:
            try:
                self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
                new_file = not self.telemetry_path.exists()
                with self.telemetry_path.open("a", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream)
                    if new_file:
                        writer.writerow(("observed_at_utc", "battery_percent", "charging", "usb_powered", "wifi_rssi"))
                    writer.writerow((self.device_last_seen.isoformat(timespec="seconds"),
                                     self.device_battery_percent, self.device_charging,
                                     self.device_usb_powered, self.device_wifi_rssi))
            except OSError:
                pass

    def status(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        age = int((now - self.device_last_seen).total_seconds()) if self.device_last_seen else None
        return {
            "service": {"running": True, "started_at_local": self.started_at.astimezone().isoformat(timespec="seconds")},
            "codex": {"connected": self.codex_connected, "error": self.error,
                      "last_updated_local": self.last_updated_local, "quota": self.quota},
            "device": {"connected_recently": age is not None and age <= 600,
                       "last_seen_local": self.device_last_seen.astimezone().isoformat(timespec="seconds")
                       if self.device_last_seen else None,
                       "last_seen_seconds_ago": age,
                       "battery_percent": self.device_battery_percent,
                       "charging": self.device_charging,
                       "usb_powered": self.device_usb_powered,
                       "wifi_rssi": self.device_wifi_rssi},
            "voice": {"state": self.voice_state, "last_request_local": self.last_voice_local,
                      "provider": self.voice_provider},
            "notifications": {"count": len(self.notifications), "items": self.notifications},
            "gmail": {"connected": self.gmail_snapshot is not None and self.gmail_error is None,
                      "error": self.gmail_error, "snapshot": self.gmail_snapshot},
            "calendar": {"connected": self.calendar_snapshot is not None and self.calendar_error is None,
                         "error": self.calendar_error, "snapshot": self.calendar_snapshot},
            "codex_sessions": {"error": self.codex_sessions_error, "items": self.codex_sessions},
        }


class QuotaWorker:
    def __init__(self, state: StateStore, poll_seconds: int = 300, executable: str | None = None) -> None:
        self.state = state
        self.poll_seconds = poll_seconds
        self.executable = executable
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        delay = 2
        while not self._stop.is_set():
            rpc = CodexAppServer(self.executable)
            try:
                await rpc.start()
                delay = 2
                while rpc.alive and not self._stop.is_set():
                    rpc.updated.clear()
                    account = await rpc.request("account/read")
                    limits = await rpc.request("account/rateLimits/read", {"excludeResetCreditDetails": True})
                    self.state.update(normalize_quota(account, limits), True, None)
                    try:
                        sessions = await asyncio.wait_for(recent_sessions(rpc, limit=5), timeout=30)
                        self.state.update_codex_sessions([item.as_dict() for item in sessions], None)
                    except (Exception, asyncio.TimeoutError):
                        self.state.update_codex_sessions(None, "Recent Codex task status unavailable")
                    try:
                        await asyncio.wait_for(rpc.updated.wait(), timeout=self.poll_seconds)
                    except asyncio.TimeoutError:
                        pass
                if not self._stop.is_set():
                    raise RuntimeError("Codex App Server disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Preserve the last good snapshot but visibly mark it stale.
                self.state.update(self.state.quota, False, type(exc).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 300)
            finally:
                await rpc.close()
