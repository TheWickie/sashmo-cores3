import asyncio
import io
import wave

import pytest

from sashmo import speech
from sashmo.speech import answer_command, normalize_reply_wav, validate_wav


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


def test_longer_questions_fit_the_new_voice_limit():
    def wav_seconds(seconds):
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\0\0" * (16000 * seconds))
        return output.getvalue()

    validate_wav(wav_seconds(12))
    with pytest.raises(ValueError, match="duration limit"):
        validate_wav(wav_seconds(21))


def test_local_speech_falls_back_when_reply_exceeds_device_limit(monkeypatch):
    def fake_bridge(mode, input_path, output_path):
        words = input_path.read_text(encoding="utf-8")
        seconds = 21 if not words.startswith("That answer was too long") else 2
        with wave.open(str(output_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\0\0" * (16000 * seconds))

    monkeypatch.setattr(speech, "_run_bridge", fake_bridge)
    result = speech.synthesize("A " * 150)
    validate_wav(result)
    with wave.open(io.BytesIO(result), "rb") as reader:
        assert reader.getnframes() == 32000


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


def test_private_reply_uses_cedar_path_instead_of_windows_voice(monkeypatch):
    async def fake_transcribe(_audio):
        return "Discuss my task", 1.0

    async def cedar(answer):
        assert answer == "A private task summary"
        return b"cedar"

    class Provider:
        sensitive_output = True

        async def respond(self, *_args):
            return "A private task summary"

    monkeypatch.setattr(speech, "transcribe", fake_transcribe)
    monkeypatch.setattr(speech, "synthesize_reply", cedar)
    monkeypatch.setattr(speech, "synthesize", lambda _: (_ for _ in ()).throw(
        AssertionError("Windows speech must not be selected")))
    assert asyncio.run(speech.process_audio(b"test", None, True, Provider()))[2] == b"cedar"


def test_silent_follow_up_ends_without_speech_synthesis(monkeypatch):
    async def no_speech(_audio):
        return None, 0.0

    async def synthesize_must_not_run(_answer):
        raise AssertionError("silent follow-up must not synthesize")

    monkeypatch.setattr(speech, "transcribe", no_speech)
    monkeypatch.setattr(speech, "synthesize_reply", synthesize_must_not_run)
    assert asyncio.run(speech.process_audio(b"test", QUOTA, True,
                                            silent_on_no_speech=True)) == ("", "", b"")
