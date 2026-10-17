import asyncio
import json
import subprocess

from sashmo.conversation import ConversationProvider


class FakePlanClient:
    def __init__(self):
        self.requests = []

    def active_selection(self):
        return "account", "listed-model"

    def stream_text(self, key, model, messages):
        self.requests.append((key, model, messages))
        return "A short spoken reply."


def test_local_quota_follow_up_uses_no_plan_inference(tmp_path):
    client = FakePlanClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client)
    quota = {"primary_bucket_id": "codex", "buckets": [{"id": "codex", "windows": [
        {"duration_mins": 10080, "remaining_percent": 50,
         "resets_at_local": "2026-10-17T10:00:00+01:00"}]}]}
    first = asyncio.run(provider.respond("How is my Codex quota?", quota, True))
    follow_up = asyncio.run(provider.respond("When does that reset?", quota, True))
    assert "50 percent" in first
    assert "resets on" in follow_up
    assert provider.last_source == "local"
    assert client.requests == []


def test_plan_conversation_keeps_bounded_context_without_actions(tmp_path):
    client = FakePlanClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client)
    assert asyncio.run(provider.respond("Tell me a short fact.", None, False)) == "A short spoken reply."
    assert asyncio.run(provider.respond("Tell me another.", None, False)) == "A short spoken reply."
    assert provider.last_source == "chatgpt_plan"
    messages = client.requests[-1][2]
    assert messages[-3:] == [
        {"role": "user", "content": "Tell me a short fact."},
        {"role": "assistant", "content": "A short spoken reply."},
        {"role": "user", "content": "Tell me another."},
    ]
    assert "no tools" in messages[0]["content"].lower()


def test_mail_answers_use_cached_metadata_and_local_voice_only(tmp_path):
    class State:
        gmail_snapshot = {"unread_count": 3, "messages": [
            {"from": "Example Sender", "subject": "Example Subject"}]}
    client = FakePlanClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client, state=State())
    reply = asyncio.run(provider.respond("What is my latest email?", None, False))
    assert "Example Sender" in reply
    assert provider.sensitive_output is True
    assert provider.last_source == "local"
    assert client.requests == []


def test_stale_mail_answer_does_not_present_cached_count_as_current(tmp_path):
    class State:
        gmail_snapshot = {"unread_count": 3, "messages": []}
        gmail_error = "Gmail unavailable"
    provider = ConversationProvider(tmp_path / "projects.local.json", client=FakePlanClient(), state=State())
    reply = asyncio.run(provider.respond("How many emails?", None, True))
    assert "temporarily unavailable" in reply
    assert "3 unread" not in reply


def test_calendar_answers_stay_local_and_do_not_invoke_plan(tmp_path):
    from datetime import datetime, timedelta
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    class State:
        calendar_snapshot = {"events": [{"title": "Example appointment", "start": tomorrow,
                                          "end": tomorrow, "all_day": True}]}
    client = FakePlanClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client, state=State())
    reply = asyncio.run(provider.respond("What's on tomorrow?", None, False))
    assert "Example appointment" in reply
    assert provider.sensitive_output is True
    assert provider.last_source == "local"
    assert client.requests == []


def test_codex_activity_answer_uses_bounded_status_without_plan(tmp_path):
    class State:
        codex_sessions_error = None
        codex_sessions = [{"label": "Codex task 1", "state": "in_progress"},
                          {"label": "Codex task 2", "state": "completed"}]
    client = FakePlanClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client, state=State())
    reply = asyncio.run(provider.respond("What's Codex doing?", None, True))
    assert "1 is active" in reply
    assert "2 recent Codex tasks" in reply
    assert client.requests == []


def test_named_project_discussion_sends_only_user_briefing_and_git_status(tmp_path):
    repo = tmp_path / "sample"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "README.md").write_text("private source context", encoding="utf-8")
    config = tmp_path / "projects.local.json"
    config.write_text(json.dumps({"allowed_roots": [str(tmp_path)], "projects": [
        {"id": "sample", "name": "Sample", "path": str(repo),
         "briefing": "A planning tool; next task is a better dashboard."}
    ]}), encoding="utf-8")
    client = FakePlanClient()
    provider = ConversationProvider(config, client=client)

    reply = asyncio.run(provider.respond("What should we work on next in Sample?", None, True))
    assert reply == "A short spoken reply."
    assert provider.sensitive_output is True
    messages = client.requests[-1][2]
    sent = json.dumps(messages)
    assert "A planning tool" in sent
    assert "private source context" not in sent
    assert str(repo) not in sent

    provider.sensitive_output = False
    local = asyncio.run(provider.respond("What is Sample's Git status?", None, True))
    assert "uncommitted changes" in local
    assert provider.sensitive_output is False
    assert len(client.requests) == 1
