import asyncio

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
