import asyncio
import io
import wave

from sashmo import speech
from sashmo.speech import answer_command, normalize_reply_wav


QUOTA = {
    "primary_bucket_id": "codex",
    "buckets": [{"id": "codex", "windows": [{
        "slot": "primary", "remaining_percent": 54, "duration_mins": 10080,
        "resets_at_local": "2026-10-22T07:12:37+01:00",
    }]}],
}


def test_quota_answer_uses_only_actual_window():
    answer = answer_command("How much Codex quota do I have left", QUOTA, True)
    assert "weekly" in answer
    assert "54 percent" in answer
    assert "five" not in answer


def test_unknown_request_cannot_execute_actions():
    answer = answer_command("Delete every file", QUOTA, True)
    assert "I can report" in answer


def test_streaming_wav_header_is_normalized_for_device():
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\x00\x00" * 2400)
    streaming = bytearray(output.getvalue())
    streaming[4:8] = (2**31 - 1).to_bytes(4, "little")
    streaming[40:44] = (2**31 - 1).to_bytes(4, "little")

    normalized = normalize_reply_wav(bytes(streaming))
    with wave.open(io.BytesIO(normalized), "rb") as reader:
        assert reader.getframerate() == 24000
        assert reader.getnframes() == 2400


def test_text_provider_can_be_replaced_without_changing_speech_transport(monkeypatch):
    async def fake_transcribe(_audio):
        return "hello", 1.0

    async def fake_synthesize(answer):
        return answer.encode()

    class Provider:
        async def respond(self, transcript, quota, connected):
            assert quota is QUOTA and connected
            return f"Received {transcript}"

    monkeypatch.setattr(speech, "transcribe", fake_transcribe)
    monkeypatch.setattr(speech, "synthesize_reply", fake_synthesize)
    assert asyncio.run(speech.process_audio(b"test", QUOTA, True, Provider())) == (
        "hello", "Received hello", b"Received hello"
    )
