"""Pure, bounded projection of the existing Sashmo service cache into 15 keys."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from urllib.parse import urlparse


@dataclass(frozen=True)
class Card:
    title: str
    detail: str
    kind: str
    accent: str
    action: str = ""
    target: str = ""


def valid_chat_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname in {"chatgpt.com", "www.chatgpt.com"} \
        and parsed.path.startswith("/c/") and len(parsed.path) > 3 and not parsed.username


def load_settings(root: Path) -> dict:
    path = root / "deck.local.json"
    if not path.exists():
        return {"projects": [], "chats": [], "brightness": 55, "privacy": False}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Deck settings must be an object")
    projects = raw.get("projects", [])
    chats = raw.get("chats", [])
    brightness = raw.get("brightness", 55)
    if not isinstance(projects, list) or len(projects) > 2 or not all(
        isinstance(p, dict) and isinstance(p.get("name"), str) and 1 <= len(p["name"]) <= 48
        and isinstance(p.get("path", ""), str) for p in projects):
        raise ValueError("Deck projects must be a list of up to two named folders")
    if not isinstance(chats, list) or len(chats) > 3 or not all(
        isinstance(c, dict) and isinstance(c.get("title"), str)
        and 1 <= len(c["title"]) <= 48 and valid_chat_url(c.get("url", "")) for c in chats):
        raise ValueError("Deck chats must be up to three saved ChatGPT conversation URLs")
    if type(brightness) is not int or not 0 <= brightness <= 100:
        raise ValueError("Deck brightness must be 0 through 100")
    return {"projects": projects, "chats": chats, "brightness": brightness,
            "privacy": raw.get("privacy") is True}


def _codex_bucket(status: dict) -> list[dict]:
    quota = (status.get("codex") or {}).get("quota") or {}
    for bucket in quota.get("buckets") or []:
        if bucket.get("id") == "codex":
            return bucket.get("windows") or []
    return []


def _weekly(status: dict) -> str:
    for window in _codex_bucket(status):
        if window.get("duration_mins") == 10080:
            value = window.get("remaining_percent")
            return f"{value}% left" if isinstance(value, (int, float)) else "Unavailable"
    return "Not reported"


def cards(status: dict, settings: dict) -> list[Card]:
    service_available = (status.get("service") or {}).get("running") is True
    projects = settings.get("projects") or []
    chats = settings.get("chats") or []
    vscode = ((status.get("vscode") or {}).get("items") or [])[:3]
    sessions = ((status.get("codex_sessions") or {}).get("items") or [])
    running = sum(item.get("state") == "in_progress" for item in sessions)
    unread = ((status.get("gmail") or {}).get("snapshot") or {}).get("unread_count")
    events = ((status.get("calendar") or {}).get("snapshot") or {}).get("events") or []
    alerts = status.get("deck_notifications") or []
    unread_alerts = status.get("deck_unread_count")
    if not isinstance(unread_alerts, int) or unread_alerts < 0:
        unread_alerts = sum(item.get("read") is False for item in alerts)
    device = status.get("device") or {}
    voice = status.get("voice") or {}
    age = device.get("last_seen_seconds_ago")
    device_live = isinstance(age, int) and 0 <= age <= 90
    low_battery = (device.get("usb_powered") is False and isinstance(device.get("battery_percent"), int)
                   and device["battery_percent"] <= 15)

    def project(i: int) -> Card:
        if i >= len(projects):
            return Card(f"PROJECT {i+1}", "Set folder", "project", "#2ce4b5")
        item = projects[i]
        return Card(item["name"], "Open folder", "project", "#2ce4b5", "folder", item.get("path", ""))

    def chat(i: int) -> Card:
        if i >= len(chats):
            return Card(f"CHAT {i+1:02}", "Register link", "chat", "#a479ff")
        return Card(chats[i]["title"], "Saved chat", "chat", "#a479ff", "url", chats[i]["url"])

    def codex(i: int) -> Card:
        if i >= len(vscode):
            return Card(f"CODEX {i+1:02}", "No VS Code task", "codex", "#4db6ff")
        item = vscode[i]
        return Card(item.get("title") or "Codex task", "VS Code recent", "codex", "#4db6ff",
                    "codex", item.get("thread_id", ""))

    return [
        project(0), project(1),
        Card("GMAIL", f"{unread} unread" if isinstance(unread, int) and (status.get("gmail") or {}).get("connected") else "Unavailable", "mail", "#ff7768", "url", "https://mail.google.com/mail/u/0/#inbox"),
        chat(0), codex(0),
        Card("CODEX TASKS", f"{running} running" if service_available and (status.get("codex_sessions") or {}).get("error") is None else "Unavailable", "tasks", "#42b9ff", "status"),
        Card("WEEKLY", _weekly(status), "quota", "#27e3c2", "status"),
        Card("CALENDAR", f"{len(events)} upcoming" if (status.get("calendar") or {}).get("connected") else "Unavailable", "calendar", "#f6a64c", "url", "https://calendar.google.com/calendar/u/0/r"),
        chat(1), codex(1),
        Card("ALERTS", f"{unread_alerts} new" if service_available else "Unavailable", "alerts", "#ffb450", "alerts"),
        Card("SASHMO", "Connected" if device_live else "Offline", "device", "#2ce4a0" if device_live else "#ff7768", "status"),
        Card("TALK", "When ready" if device_live and voice.get("state") == "idle" and not low_battery else "Offline / busy", "talk", "#44eadc", "talk"),
        chat(2), codex(2),
    ]
