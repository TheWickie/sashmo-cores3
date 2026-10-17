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


def test_explicit_detailed_answer_is_saved_without_short_reply_cap(tmp_path):
    class LongClient(FakePlanClient):
        def stream_text(self, key, model, messages):
            self.requests.append((key, model, messages))
            return "This is a detailed paragraph about the design. " * 35

    client = LongClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client)
    answer = asyncio.run(provider.respond("Give me a detailed explanation of the design", None, True))
    assert len(answer) > 1000
    assert provider.long_reply is True
    assert "600 to 1,000 words" in client.requests[-1][2][0]["content"]
    restarted = ConversationProvider(tmp_path / "projects.local.json", client=client)
    assert restarted.turns[-1]["content"] == answer


def test_time_word_in_general_question_does_not_hijack_plan_conversation(tmp_path):
    client = FakePlanClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client)
    reply = asyncio.run(provider.respond("What do you think of time travel?", None, True))
    assert reply == "A short spoken reply."
    assert provider.last_source == "chatgpt_plan"
    assert len(client.requests) == 1


def test_volume_command_is_local_and_exposes_only_valid_percent(tmp_path):
    client = FakePlanClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client)
    assert asyncio.run(provider.respond("Set volume to forty five percent", None, True)) == (
        "Volume set to 45 percent.")
    assert provider.volume_percent == 45
    assert provider.last_source == "local"
    assert client.requests == []
    assert "zero to one hundred" in asyncio.run(provider.respond(
        "Set volume to 150 percent", None, True))
    assert provider.volume_percent is None
    assert asyncio.run(provider.respond("Set volume to 0%", None, True)) == (
        "Volume set to 0 percent.")
    assert provider.volume_percent == 0
    assert asyncio.run(provider.respond("Can you set your volume to 40 percent?", None, True)) == (
        "Volume set to 40 percent.")
    assert provider.volume_percent == 40
    assert asyncio.run(provider.respond("Please adjust the speaker volume at 55%", None, True)) == (
        "Volume set to 55 percent.")
    assert provider.volume_percent == 55


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
    reply = asyncio.run(provider.respond("How many active Codex tasks?", None, True))
    assert "1 is active" in reply
    assert "2 recent Codex tasks" in reply
    assert client.requests == []


def test_recent_codex_projects_answer_stays_local_and_sensitive(tmp_path):
    class State:
        codex_projects_error = None
        codex_projects = [{"name": "Sashmo"}, {"name": "Another"}]
    client = FakePlanClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client, state=State())
    reply = asyncio.run(provider.respond("What are my recent project folders?", None, True))
    assert "Sashmo" in reply and "Another" in reply
    assert provider.sensitive_output is True
    assert client.requests == []


def test_recent_codex_work_and_named_task_discussion(monkeypatch, tmp_path):
    class FakeRpc:
        async def start(self):
            pass

        async def close(self):
            pass

        async def request(self, method, params=None, timeout=30):
            if method == "thread/list":
                return {"data": [{"id": "task-1", "source": "vscode",
                                  "preview": "Plan a small rover", "recencyAt": 100},
                                 {"id": "task-2", "source": "cli",
                                  "name": "Fix weather display", "recencyAt": 90}]}
            assert method == "thread/turns/list" and params["threadId"] == "task-1"
            return {"data": [{"items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "Can it be electric?"}]},
                {"type": "commandExecution", "aggregatedOutput": "private output"},
                {"type": "agentMessage", "phase": "final_answer", "text": "Yes, a small battery works."},
            ]}]}

    monkeypatch.setattr("sashmo.conversation.CodexAppServer", FakeRpc)
    client = FakePlanClient()
    provider = ConversationProvider(tmp_path / "projects.local.json", client=client)
    listing = asyncio.run(provider.respond("List my recent Codex work", None, True))
    assert "Plan a small rover" in listing and "Fix weather display" in listing
    assert provider.sensitive_output is True
    answer = asyncio.run(provider.respond("Tell me about the rover task", None, True))
    assert answer == "A short spoken reply."
    sent = json.dumps(client.requests[-1][2])
    assert "Can it be electric?" in sent and "small battery" in sent
    assert "private output" not in sent
    assert provider.sensitive_output is True
    assert provider.last_source == "chatgpt_plan"
    assert (tmp_path / "conversation.local.json").exists()
    after_restart = ConversationProvider(tmp_path / "projects.local.json", client=client)
    assert after_restart._active_codex_title == "Plan a small rover"
    assert len(after_restart.turns) >= 2
    encoded = provider.compact_device_memory()
    assert len(encoded) < 900
    recovered = ConversationProvider(tmp_path / "fresh" / "projects.local.json", client=client)
    recovered.restore_device_memory(encoded)
    assert recovered._active_codex_title == "Plan a small rover"
    assert recovered.turns


def test_new_conversation_clears_old_context_and_rejects_stale_device_memory(tmp_path):
    client = FakePlanClient()
    project_file = tmp_path / "projects.local.json"
    provider = ConversationProvider(project_file, client=client)
    asyncio.run(provider.respond("Tell me a short fact", None, True))
    old_id = provider.conversation_id
    old_device_memory = provider.compact_device_memory()
    answer = asyncio.run(provider.respond("Start a new conversation", None, True))
    assert "new Sashmo conversation" in answer
    assert provider.conversation_id != old_id
    assert not provider.turns
    restarted = ConversationProvider(project_file, client=client)
    restarted.restore_device_memory(old_device_memory)
    assert restarted.conversation_id == provider.conversation_id
    assert not restarted.turns


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
