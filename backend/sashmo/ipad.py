"""Read-only iPad dashboard projection, pairing and notification history.

This module never returns credentials, conversation text, project paths or mail
bodies. The existing CoreS3 and desktop endpoints remain separate.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _short_hash(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()[:16]


def _safe_text(value: Any, limit: int = 140) -> str:
    return " ".join(str(value or "").split())[:limit]


class IpadPairing:
    """Short-lived local pairing code; signed browser cookie survives restarts."""

    def __init__(self, device_token: str, bind_host: str) -> None:
        self._key = hashlib.sha256(("sashmo-ipad-v1:" + device_token).encode()).digest()
        self._network = ipaddress.ip_network(f"{bind_host}/24", strict=False)
        self._code = ""
        self._expires_at = 0.0
        self._failed: dict[str, tuple[int, float]] = {}
        self._lock = threading.RLock()

    def on_home_network(self, peer: str) -> bool:
        try:
            address = ipaddress.ip_address(peer)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            return address in self._network
        except ValueError:
            return False

    def current_code(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            if now >= self._expires_at:
                self._code = f"{secrets.randbelow(100_000_000):08d}"
                self._expires_at = now + 600
            return {"code": self._code, "expires_at": int(self._expires_at)}

    def pair(self, code: str, peer: str) -> str | None:
        if not self.on_home_network(peer):
            return None
        with self._lock:
            now = time.time()
            failures, until = self._failed.get(peer, (0, now + 900))
            if now >= until:
                failures, until = 0, now + 900
            if failures >= 5:
                return None
            expected = self.current_code()["code"]
            if not isinstance(code, str) or not hmac.compare_digest(code, expected):
                self._failed[peer] = (failures + 1, until)
                return None
            self._failed.pop(peer, None)
            expires = int(now + 30 * 86400)
            payload = f"1:{expires}:{secrets.token_urlsafe(18)}".encode()
            body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
            signature = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
            return body + "." + signature

    def valid(self, token: str | None, peer: str) -> bool:
        if not token or not self.on_home_network(peer) or len(token) > 256:
            return False
        try:
            body, signature = token.split(".", 1)
            expected = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return False
            payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
            version, expires, _nonce = payload.split(":", 2)
            return version == "1" and int(expires) > time.time()
        except (UnicodeError, ValueError, OverflowError, binascii.Error):
            return False


DEFAULT_PREFERENCES = {"minimum_priority": "normal", "live_alerts": True,
                       "night_dim": True, "night_start": "22:00", "night_end": "07:00"}
PRIORITIES = {"low": 0, "normal": 1, "high": 2, "critical": 3}


class IpadNotificationStore:
    """Small desktop-only history; source IDs and read state never reach CoreS3."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.items: list[dict[str, Any]] = []
        self.preferences = dict(DEFAULT_PREFERENCES)
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.items = data.get("items", [])[:300]
                self.preferences.update(data.get("preferences", {}))
            except (OSError, ValueError, TypeError):
                pass

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"items": self.items[:300],
                                         "preferences": self.preferences}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def ingest(self, candidates: list[dict[str, Any]]) -> None:
        with self._lock:
            known = {item["id"] for item in self.items}
            changed = False
            for candidate in candidates:
                key = _safe_text(candidate.get("key"), 90)
                if not key:
                    continue
                identifier = hashlib.sha256(key.encode()).hexdigest()[:24]
                if identifier in known:
                    continue
                source = _safe_text(candidate.get("source"), 24)
                title = _safe_text(candidate.get("title"), 100)
                summary = _safe_text(candidate.get("summary"), 170)
                if not title:
                    continue
                priority = candidate.get("priority")
                if priority not in PRIORITIES:
                    priority = "high" if key == "battery_low" or source == "ALERT" else (
                        "normal" if source in {"MAIL", "CALENDAR", "CODEX"} else "low")
                self.items.insert(0, {"id": identifier, "source": source, "title": title,
                                      "summary": summary, "priority": priority, "read": False,
                                      "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                known.add(identifier)
                changed = True
            if changed:
                self.items = self.items[:300]
                self._save()

    def list_items(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.items[:100]]

    def mark_read(self, identifier: str, read: bool) -> bool:
        with self._lock:
            for item in self.items:
                if item["id"] == identifier:
                    if item["read"] != read:
                        item["read"] = read
                        self._save()
                    return True
            return False

    def set_preferences(self, updates: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_PREFERENCES)
        if set(updates) - allowed:
            raise ValueError("Unknown preference")
        candidate = dict(self.preferences)
        candidate.update(updates)
        if not isinstance(candidate["minimum_priority"], str) or candidate["minimum_priority"] not in PRIORITIES or not all(
            isinstance(candidate[name], bool) for name in ("live_alerts", "night_dim")
        ) or not all(isinstance(candidate[name], str) and len(candidate[name]) == 5
                     and candidate[name][:2].isdigit() and candidate[name][2] == ":"
                     and candidate[name][3:].isdigit() and 0 <= int(candidate[name][:2]) < 24
                     and 0 <= int(candidate[name][3:]) < 60
                     for name in ("night_start", "night_end")):
            raise ValueError("Invalid preference")
        with self._lock:
            self.preferences = candidate
            self._save()
            return dict(self.preferences)


def project_sections(state: Any, notifications: IpadNotificationStore,
                     projects: dict[str, Any] | None = None,
                     featured_projects: tuple[str, ...] = ()) -> dict[str, Any]:
    """Bounded, allowlisted dashboard data from existing cached workers."""
    projects = projects or {"projects": [], "error": "not_configured"}
    quota = state.quota or {}
    codex = {
        "connected": state.codex_connected, "error": state.error,
        "last_updated": state.last_updated_local,
        "plan_reported": quota.get("plan_reported"),
        "buckets": (quota.get("buckets") or [])[:6],
        "reset_credits_available": quota.get("reset_credits_available"),
        "sessions": state.codex_sessions[:10] if not state.codex_sessions_error else [],
        "completed_sessions": [item for item in state.codex_sessions[:10]
                               if item.get("state") == "completed"] if not state.codex_sessions_error else [],
        "sessions_error": state.codex_sessions_error,
        "recent_work": [{"title": _safe_text(item.get("title"), 100),
                         "source": item.get("source"), "updated_at": item.get("updated_at")}
                        for item in state.codex_work[:10]] if not state.codex_work_error else [],
        "work_error": state.codex_work_error,
    }
    reminders_current = (state.chatgpt_error is None and state.chatgpt_checked_at is not None
                         and time.time() - state.chatgpt_checked_at.timestamp() < 3600)
    chatgpt = {"reminders": state.chatgpt_reminders[:20] if reminders_current else [],
               "reminders_error": state.chatgpt_error if not reminders_current else None,
               "checked_at": (state.chatgpt_checked_at.isoformat(timespec="seconds")
                              if state.chatgpt_checked_at else None),
               "notifications_note": "ChatGPT inbox notifications are not available from this connection.",
               "account_note": "Account details are not supplied by the reminder connection."}
    registered = projects.get("projects") or []
    development = {"named_projects": [
        {"name": name, "status": next((project for project in registered
                                         if name.casefold() in str(project.get("name", "")).casefold()), None)}
        for name in featured_projects[:6]],
        "projects_error": projects.get("error"),
        "active_sessions": [item for item in codex["sessions"] if item.get("state") == "in_progress"],
        "recent_work": codex["recent_work"][:6]}
    gmail_ok = state.gmail_error is None and state.gmail_snapshot is not None
    calendar_ok = state.calendar_error is None and state.calendar_snapshot is not None
    communications = {
        "gmail": {"connected": gmail_ok, "error": state.gmail_error,
                  "unread_count": (state.gmail_snapshot or {}).get("unread_count") if gmail_ok else None,
                  "messages": [{"from": _safe_text(item.get("from"), 90),
                                "subject": _safe_text(item.get("subject"), 120),
                                "date": _safe_text(item.get("date"), 60),
                                "unread": bool(item.get("unread")),
                                "important": item.get("important") is True}
                               for item in (state.gmail_snapshot or {}).get("messages", [])[:8]
                               if item.get("unread")] if gmail_ok else []},
        "calendar": {"connected": calendar_ok, "error": state.calendar_error,
                     "events": [{"title": _safe_text(item.get("title"), 110),
                                 "start": _safe_text(item.get("start"), 50),
                                 "all_day": bool(item.get("all_day"))}
                                for item in (state.calendar_snapshot or {}).get("events", [])[:10]]
                     if calendar_ok else []},
    }
    system = {"started_at": state.started_at.isoformat(timespec="seconds"),
              "device_last_seen": (state.device_last_seen.isoformat(timespec="seconds")
                                   if state.device_last_seen else None),
              "device": {"battery_percent": state.device_battery_percent,
                         "charging": state.device_charging,
                         "usb_powered": state.device_usb_powered,
                         "wifi_rssi": state.device_wifi_rssi,
                         "screen_sleeping": state.device_screen_sleeping},
              "voice_state": state.voice_state}
    return {"codex": codex, "chatgpt": chatgpt, "development": development,
            "communications": communications, "system": system,
            "notifications": {"items": notifications.list_items(),
                              "preferences": dict(notifications.preferences)}}


def changes(sections: dict[str, Any], previous: dict[str, str]) -> dict[str, Any]:
    versions = {name: _short_hash(value) for name, value in sections.items()}
    return {"versions": versions,
            "sections": {name: value for name, value in sections.items()
                         if previous.get(name) != versions[name]}}
