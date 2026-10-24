from __future__ import annotations

import pytest

from sashmo.chatgpt_reminders import parse_task_reply, validate_chat_url
from sashmo.state import StateStore


def test_chatgpt_reply_requires_complete_structured_list():
    text = '```json\n{"tasks":[{"title":"Morning check","schedule":"Daily at 08:00"}]}\n```'
    assert parse_task_reply(text) == [{"title": "Morning check", "schedule": "Daily at 08:00"}]
    with pytest.raises(ValueError):
        parse_task_reply('The active task is probably Morning check')
    with pytest.raises(ValueError):
        parse_task_reply('{"error":"unavailable"}')


def test_chat_url_is_pinned_to_a_chatgpt_conversation():
    url = "https://chatgpt.com/c/00000000-0000-4000-8000-000000000000"
    assert validate_chat_url(url) == url
    with pytest.raises(ValueError):
        validate_chat_url("https://evil.example/c/00000000-0000-4000-8000-000000000000")


def test_notification_feed_updates_version_and_contains_read_only_sources():
    state = StateStore()
    state.update_gmail({"unread_count": 1, "messages": [{"id": "m1", "subject": "Hello",
                         "from": "friend@example.com", "unread": True}]}, None)
    state.update_calendar({"events": [{"title": "Meeting", "start": "2026-10-24T10:00:00+01:00"}]}, None)
    state.update_chatgpt_reminders([{"title": "Take medication", "schedule": "Daily at 09:00"}], None)
    version = state.version
    feed = state.pulse(-1)["notifications"]
    assert {item["source"] for item in feed} == {"MAIL", "CALENDAR", "GPT REMINDER"}
    assert "body" not in str(feed).lower()
    assert "notifications" not in state.pulse(version)
    state.update_chatgpt_reminders([{"title": "Take medication", "schedule": "Daily at 10:00"}], None)
    assert state.version > version


def test_busy_mail_does_not_hide_reminders_or_calendar():
    state = StateStore()
    state.update_gmail({"messages": [{"id": str(i), "subject": f"Mail {i}", "unread": True}
                                     for i in range(10)]}, None)
    state.update_calendar({"events": [{"title": f"Event {i}"} for i in range(4)]}, None)
    state.update_chatgpt_reminders([{"title": f"Task {i}", "schedule": "Daily"}
                                    for i in range(8)], None)
    sources = {item["source"] for item in state.notification_feed()}
    assert {"MAIL", "CALENDAR", "GPT REMINDER"} <= sources


def test_expired_sources_are_shown_as_reconnect_actions_not_current_data():
    state = StateStore()
    state.update_gmail({"unread_count": 26, "messages": [{"id": "m1", "subject": "Private mail",
                        "unread": True}]}, None)
    state.update_calendar({"events": [{"title": "Private event"}]}, None)
    state.update_gmail(None, "Gmail unavailable; reconnect in the dashboard")
    state.update_calendar(None, "Calendar unavailable; reconnect in the dashboard")
    state.update_chatgpt_reminders(None, "ChatGPT reminder sync unavailable")
    feed = state.notification_feed()
    assert {item["title"] for item in feed} == {
        "Reconnect Gmail", "Reconnect Calendar", "Reminder sync unavailable"}
    assert "Last known: 26 unread" in str(feed)
    assert "Private mail" not in str(feed) and "Private event" not in str(feed)
