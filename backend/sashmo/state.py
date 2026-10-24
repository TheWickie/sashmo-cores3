"""Versioned device state and an independently refreshed Codex quota cache."""

from __future__ import annotations

import asyncio
import csv
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .codex_rpc import CodexAppServer
from .codex_sessions import recent_sessions, recent_projects, recent_work
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
        self.device_screen_sleeping: bool | None = None
        self.voice_state = "idle"
        self.voice_provider = "local"
        self.last_voice_local: str | None = None
        self.wake_checks = 0
        self.wake_detections = 0
        self.last_wake_local: str | None = None
        self.billing_spent_usd: Decimal | None = None
        self.billing_budget_usd: Decimal | None = None
        self.billing_as_of: datetime | None = None
        self.billing_error: str | None = "OpenAI billing monitor is not configured"
        self.notifications: list[dict[str, str]] = []
        self.chatgpt_reminders: list[dict[str, str]] = []
        self.chatgpt_checked_at: datetime | None = None
        self.chatgpt_error: str | None = "Not connected"
        self.gmail_snapshot: dict[str, Any] | None = None
        self.gmail_error: str | None = "Not connected"
        self.calendar_snapshot: dict[str, Any] | None = None
        self.calendar_error: str | None = "Not connected"
        self.codex_sessions: list[dict[str, Any]] = []
        self.codex_sessions_error: str | None = "Waiting for Codex"
        self.codex_projects: list[dict[str, Any]] = []
        self.codex_projects_error: str | None = "Waiting for Codex"
        self.codex_work: list[dict[str, Any]] = []
        self.codex_work_error: str | None = "Waiting for Codex"
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
            "notification_count": len(self.notification_feed()),
            "notifications": self.notification_feed(),
            "chatgpt_checked_at": (self.chatgpt_checked_at.astimezone().isoformat(timespec="minutes")
                                   if self.chatgpt_checked_at else None),
            "gmail_unread_count": ((self.gmail_snapshot or {}).get("unread_count")
                                   if self.gmail_snapshot is not None and self.gmail_error is None else None),
            "calendar_event_count": (len((self.calendar_snapshot or {}).get("events") or [])
                                     if self.calendar_snapshot is not None and self.calendar_error is None else None),
        }

    def update_billing(self, spent: Decimal | None, budget: Decimal | None,
                       as_of: datetime | None, error: str | None) -> None:
        if (spent, budget, error) != (self.billing_spent_usd, self.billing_budget_usd,
                                     self.billing_error):
            self.version += 1
        self.billing_spent_usd = spent
        self.billing_budget_usd = budget
        self.billing_as_of = as_of
        self.billing_error = error

    def billing_pulse(self) -> dict[str, Any]:
        spent = self.billing_spent_usd
        budget = self.billing_budget_usd
        remaining = max(Decimal("0"), budget - spent) if spent is not None and budget is not None else None
        return {"spent_usd": f"{spent:.2f}" if spent is not None else None,
                "budget_left_usd": f"{remaining:.2f}" if remaining is not None else None,
                "as_of_local": self.billing_as_of.astimezone().isoformat(timespec="minutes")
                if self.billing_as_of else None,
                "error": self.billing_error}

    def update_gmail(self, snapshot: dict[str, Any] | None, error: str | None) -> None:
        previous = self.gmail_snapshot
        previous_error = self.gmail_error
        if snapshot is not None:
            self.gmail_snapshot = snapshot
        elif error == "Not connected":
            self.gmail_snapshot = None
        self.gmail_error = error
        if self.gmail_snapshot != previous or error != previous_error:
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

    def update_codex_projects(self, projects: list[dict[str, Any]] | None,
                              error: str | None) -> None:
        if projects is not None:
            self.codex_projects = projects
        self.codex_projects_error = error

    def update_codex_work(self, work: list[dict[str, Any]] | None,
                          error: str | None) -> None:
        if work is not None and work != self.codex_work:
            self.version += 1
        if work is not None:
            self.codex_work = work
        self.codex_work_error = error

    def update_chatgpt_reminders(self, reminders: list[dict[str, str]] | None,
                                 error: str | None) -> None:
        if reminders is not None:
            cleaned = [{"title": str(item["title"])[:80],
                        "schedule": str(item.get("schedule", ""))[:120]}
                       for item in reminders[:20] if item.get("title")]
            if cleaned != self.chatgpt_reminders or error != self.chatgpt_error:
                self.version += 1
            self.chatgpt_reminders = cleaned
            self.chatgpt_checked_at = datetime.now(timezone.utc)
        elif error != self.chatgpt_error:
            self.version += 1
        self.chatgpt_error = error

    def notification_feed(self) -> list[dict[str, str]]:
        """Small read-only feed for the CoreS3; no mail bodies or voice transcripts."""
        feed = [{"key": item["key"], "source": "ALERT", "title": item["title"],
                 "summary": item["summary"]} for item in self.notifications]
        if self.gmail_error is None:
            for message in (self.gmail_snapshot or {}).get("messages", [])[:5]:
                if message.get("unread"):
                    feed.append({"key": "mail:" + str(message.get("id", ""))[:40],
                                 "source": "MAIL", "title": str(message.get("subject") or "Unread email")[:80],
                                 "summary": "From " + str(message.get("from") or "unknown sender")[:100]})
        elif self.gmail_error != "Not connected":
            unread = (self.gmail_snapshot or {}).get("unread_count")
            detail = (f"Last known: {unread} unread. " if isinstance(unread, int) else "")
            feed.append({"key": "mail:stale", "source": "MAIL", "title": "Reconnect Gmail",
                         "summary": detail + "Open Gmail in the Windows dashboard."})
        if self.calendar_error is None:
            for event in (self.calendar_snapshot or {}).get("events", [])[:4]:
                start = str(event.get("start", ""))
                feed.append({"key": "calendar:" + start[:40], "source": "CALENDAR",
                             "title": str(event.get("title") or "Upcoming event")[:80],
                             "summary": "Starts " + start[:35]})
        elif self.calendar_error != "Not connected":
            feed.append({"key": "calendar:stale", "source": "CALENDAR",
                         "title": "Reconnect Calendar",
                         "summary": "Upcoming events are unavailable. Open Calendar in the Windows dashboard."})
        for item in self.codex_work[:8]:
            title = str(item.get("title", ""))
            if re.search(r"\b(bug|bugfix|fix|fixes|fixed|error|failure|broken)\b", title, re.I):
                feed.append({"key": "codex:" + title[:40], "source": "CODEX",
                             "title": title[:80], "summary": "Recent Codex work; review its task for status"})
        if self.chatgpt_error is None and self.chatgpt_checked_at and (datetime.now(timezone.utc) - self.chatgpt_checked_at).total_seconds() < 3600:
            for item in self.chatgpt_reminders[:8]:
                feed.append({"key": "gpt:" + item["title"][:40], "source": "GPT REMINDER",
                             "title": item["title"], "summary": item["schedule"]})
        elif self.chatgpt_reminders:
            feed.append({"key": "gpt:stale", "source": "GPT", "title": "Reminder sync is stale",
                         "summary": "Check the signed-in ChatGPT browser"})
        elif self.chatgpt_error not in (None, "Not connected"):
            feed.append({"key": "gpt:unavailable", "source": "GPT",
                         "title": "Reminder sync unavailable",
                         "summary": "Check ChatGPT connection in Windows Updates."})
        # A busy inbox must not push Calendar and reminders off the device.
        sources: dict[str, list[dict[str, str]]] = {}
        for item in feed:
            sources.setdefault(item["source"], []).append(item)
        balanced: list[dict[str, str]] = []
        while sources and len(balanced) < 12:
            for source in list(sources):
                balanced.append(sources[source].pop(0))
                if not sources[source]:
                    del sources[source]
                if len(balanced) == 12:
                    break
        return balanced

    def notify(self, key: str, title: str, summary: str) -> None:
        """Replace one local alert by stable key; do not include credentials or transcripts."""
        item = {"key": key[:64], "title": title[:60], "summary": summary[:120]}
        if any(existing == item for existing in self.notifications):
            return
        self.notifications = [entry for entry in self.notifications if entry["key"] != item["key"]]
        self.notifications.append(item)
        self.notifications = self.notifications[-10:]
        self.version += 1

    def record_wake_check(self, detected: bool) -> None:
        self.wake_checks += 1
        if detected:
            self.wake_detections += 1
            self.last_wake_local = datetime.now().astimezone().isoformat(timespec="seconds")

    def clear_notification(self, key: str) -> None:
        remaining = [entry for entry in self.notifications if entry["key"] != key]
        if len(remaining) != len(self.notifications):
            self.notifications = remaining
            self.version += 1

    def record_device(self, battery: int | None, charging: bool | None,
                      usb: bool | None, wifi_rssi: int | None,
                      screen_sleeping: bool | None = None) -> None:
        self.device_last_seen = datetime.now(timezone.utc)
        self.device_battery_percent = battery if battery is not None and 0 <= battery <= 100 else None
        self.device_charging = charging
        self.device_usb_powered = usb
        self.device_wifi_rssi = wifi_rssi if wifi_rssi is not None and -120 <= wifi_rssi <= 0 else None
        self.device_screen_sleeping = screen_sleeping
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
                       "wifi_rssi": self.device_wifi_rssi,
                       "screen_sleeping": self.device_screen_sleeping},
            "voice": {"state": self.voice_state, "last_request_local": self.last_voice_local,
                      "provider": self.voice_provider, "wake_checks": self.wake_checks,
                      "wake_detections": self.wake_detections,
                      "last_wake_local": self.last_wake_local},
            "api_billing": self.billing_pulse(),
            "notifications": {"count": len(self.notification_feed()), "items": self.notification_feed()},
            "chatgpt_reminders": {"count": len(self.chatgpt_reminders),
                                  "checked_at_local": (self.chatgpt_checked_at.astimezone().isoformat(timespec="minutes")
                                                       if self.chatgpt_checked_at else None),
                                  "error": self.chatgpt_error},
            "gmail": {"connected": self.gmail_snapshot is not None and self.gmail_error is None,
                      "error": self.gmail_error, "snapshot": self.gmail_snapshot},
            "calendar": {"connected": self.calendar_snapshot is not None and self.calendar_error is None,
                         "error": self.calendar_error, "snapshot": self.calendar_snapshot},
            "codex_sessions": {"error": self.codex_sessions_error, "items": self.codex_sessions},
            "codex_projects": {"error": self.codex_projects_error, "items": self.codex_projects},
            "codex_work": {"error": self.codex_work_error,
                           "items": [{"title": item["title"], "source": item["source"],
                                      "updated_at": item["updated_at"]}
                                     for item in self.codex_work]},
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
                        projects = await asyncio.wait_for(recent_projects(rpc, limit=10), timeout=45)
                        self.state.update_codex_projects([item.as_dict() for item in projects], None)
                    except (Exception, asyncio.TimeoutError):
                        self.state.update_codex_projects(None, "Recent Codex projects unavailable")
                    try:
                        work = await asyncio.wait_for(recent_work(rpc, limit=10), timeout=30)
                        self.state.update_codex_work([item.as_dict() for item in work], None)
                    except (Exception, asyncio.TimeoutError):
                        self.state.update_codex_work(None, "Recent Codex work unavailable")
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
