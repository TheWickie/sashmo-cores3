"""Versioned device state and an independently refreshed Codex quota cache."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .codex_rpc import CodexAppServer
from .quota import normalize_quota


class StateStore:
    def __init__(self) -> None:
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
        self.last_voice_local: str | None = None

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
        }

    def record_device(self, battery: int | None, charging: bool | None,
                      usb: bool | None, wifi_rssi: int | None) -> None:
        self.device_last_seen = datetime.now(timezone.utc)
        self.device_battery_percent = battery if battery is not None and 0 <= battery <= 100 else None
        self.device_charging = charging
        self.device_usb_powered = usb
        self.device_wifi_rssi = wifi_rssi if wifi_rssi is not None and -120 <= wifi_rssi <= 0 else None

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
            "voice": {"state": self.voice_state, "last_request_local": self.last_voice_local},
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
