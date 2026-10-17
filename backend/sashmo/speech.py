"""Desktop speech recognition, bounded commands, and local synthesis."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import subprocess
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx


BRIDGE = Path(__file__).resolve().parents[1] / "speech_bridge.ps1"
API_KEY_FILE = Path(__file__).resolve().parents[2] / "openAIAPIKey.txt"
TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
SPEECH_URL = "https://api.openai.com/v1/audio/speech"
SPEECH_MODEL = "gpt-4o-mini-tts"
SPEECH_VOICE = "cedar"
LOG = logging.getLogger("sashmo.speech")


class TextResponseProvider(Protocol):
    """A replaceable text response path; speech transport stays unchanged."""

    async def respond(self, transcript: str, quota: dict[str, Any] | None, connected: bool) -> str: ...


class ReadOnlyCommandProvider:
    async def respond(self, transcript: str, quota: dict[str, Any] | None, connected: bool) -> str:
        return answer_command(transcript, quota, connected)


def validate_wav(data: bytes, max_seconds: int = 20) -> None:
    if len(data) > 16000 * 2 * max_seconds + 4096:
        raise ValueError("Audio exceeds the duration limit")
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1, 2, 16000):
                raise ValueError("Expected 16 kHz, mono, 16-bit PCM WAV")
            if reader.getnframes() > 16000 * max_seconds:
                raise ValueError("Audio exceeds the duration limit")
    except (wave.Error, EOFError) as exc:
        raise ValueError("Invalid WAV data") from exc


def _run_bridge(mode: str, input_path: Path, output_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(BRIDGE), "-Mode", mode, "-InputPath", str(input_path),
    ]
    if output_path:
        command.extend(["-OutputPath", str(output_path)])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(f"Windows speech {mode.lower()} failed")
    return result


def recognize(data: bytes) -> tuple[str | None, float]:
    validate_wav(data)
    with tempfile.TemporaryDirectory(prefix="sashmo-audio-") as temp:
        path = Path(temp) / "input.wav"
        path.write_bytes(data)
        result = _run_bridge("Recognize", path)
        parsed = json.loads(result.stdout.strip())
        return parsed.get("text"), float(parsed.get("confidence") or 0)


def recognize_wake(data: bytes) -> bool:
    """Check the wake phrase with Windows speech only; never use cloud STT."""
    validate_wav(data, max_seconds=3)
    with tempfile.TemporaryDirectory(prefix="sashmo-wake-") as temp:
        path = Path(temp) / "wake.wav"
        path.write_bytes(data)
        result = _run_bridge("Wake", path)
        parsed = json.loads(result.stdout.strip())
        return (str(parsed.get("text") or "").casefold() == "hello sashmo"
                and float(parsed.get("confidence") or 0) >= 0.8)


async def transcribe(data: bytes) -> tuple[str | None, float]:
    """Use the user-supplied API key for STT; retain offline recognition as fallback."""
    validate_wav(data)
    if API_KEY_FILE.is_file():
        key = API_KEY_FILE.read_text(encoding="utf-8-sig").strip()
        if key:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        TRANSCRIPTION_URL,
                        headers={"Authorization": f"Bearer {key}"},
                        data={"model": TRANSCRIPTION_MODEL},
                        files={"file": ("sashmo.wav", data, "audio/wav")},
                    )
                response.raise_for_status()
                transcript = response.json().get("text")
                if isinstance(transcript, str) and transcript.strip():
                    return transcript.strip(), 1.0
                LOG.warning("Cloud transcription returned no speech; using local recognition")
            except (httpx.HTTPError, ValueError, KeyError):
                LOG.warning("Cloud transcription unavailable; using local recognition")
    return await asyncio.to_thread(recognize, data)


def synthesize(text: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="sashmo-speech-") as temp:
        input_path = Path(temp) / "response.txt"
        output_path = Path(temp) / "response.wav"
        candidates = [text]
        for limit in (210, 130):
            if len(text) > limit:
                candidates.append(text[:limit].rsplit(" ", 1)[0].rstrip(".,; ") + ". Ask me for more.")
        candidates.append("That answer was too long to play. Please ask me for a shorter summary.")
        for candidate in candidates:
            input_path.write_text(candidate, encoding="utf-8")
            _run_bridge("Synthesize", input_path, output_path)
            data = output_path.read_bytes()
            try:
                validate_wav(data, max_seconds=20)
            except ValueError as exc:
                if "duration limit" not in str(exc):
                    raise
            else:
                return data
        raise ValueError("Speech output exceeded the device duration limit")


def normalize_reply_wav(data: bytes) -> bytes:
    """Replace streaming WAV length markers with exact lengths for the CoreS3."""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
                raise ValueError("Expected mono 16-bit PCM reply")
            rate = reader.getframerate()
            if rate not in (16000, 24000):
                raise ValueError("Unsupported reply sample rate")
            samples = reader.readframes(rate * 20 + 1)
            if len(samples) > rate * 20 * 2:
                raise ValueError("Reply audio exceeds duration limit")
    except wave.Error as exc:
        raise ValueError("Invalid reply WAV data") from exc
    if len(samples) + 44 > 700_000:
        raise ValueError("Reply audio exceeds device limit")
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples)
    return output.getvalue()


async def synthesize_reply(text: str) -> bytes:
    if API_KEY_FILE.is_file():
        key = API_KEY_FILE.read_text(encoding="utf-8-sig").strip()
        if key:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        SPEECH_URL,
                        headers={"Authorization": f"Bearer {key}"},
                        json={
                            "model": SPEECH_MODEL,
                            "voice": SPEECH_VOICE,
                            "input": text,
                            "instructions": "Speak in a calm, warm, reassuring tone. Be clear and natural.",
                            "response_format": "wav",
                        },
                    )
                response.raise_for_status()
                return normalize_reply_wav(response.content)
            except (httpx.HTTPError, ValueError):
                LOG.warning("Cloud speech output unavailable; using local voice")
    return await asyncio.to_thread(synthesize, text)


async def process_audio(data: bytes, quota: dict[str, Any] | None, connected: bool,
                        provider: TextResponseProvider | None = None,
                        silent_on_no_speech: bool = False) -> tuple[str, str, bytes]:
    transcript, confidence = await transcribe(data)
    if not transcript or confidence < 0.45:
        if silent_on_no_speech:
            return "", "", b""
        answer = "Sorry, I didn't catch that. Please tap the microphone and try again."
    else:
        answer = await (provider or ReadOnlyCommandProvider()).respond(transcript, quota, connected)
    audio = (await asyncio.to_thread(synthesize, answer)
             if provider and getattr(provider, "sensitive_output", False)
             else await synthesize_reply(answer))
    return transcript or "", answer, audio


def _windows(quota: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not quota:
        return []
    bucket_id = quota.get("primary_bucket_id")
    for bucket in quota.get("buckets") or []:
        if bucket.get("id") == bucket_id:
            return bucket.get("windows") or []
    return []


def _label(window: dict[str, Any]) -> str:
    minutes = window.get("duration_mins")
    if minutes == 10080:
        return "weekly"
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}-day"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return "reported"


def answer_command(transcript: str, quota: dict[str, Any] | None, connected: bool) -> str:
    utterance = transcript.casefold()
    now = datetime.now().astimezone()
    windows = _windows(quota)

    if "quota" in utterance or "allowance" in utterance:
        if not windows:
            return "Codex quota is unavailable right now."
        parts = [f"Your {_label(w)} Codex allowance has {w['remaining_percent']} percent remaining" for w in windows]
        return ". ".join(parts) + "."
    if "reset" in utterance:
        resets = [w for w in windows if w.get("resets_at_local")]
        if not resets:
            return "Codex has not reported a reset time."
        return ". ".join(
            f"Your {_label(w)} quota resets on {datetime.fromisoformat(w['resets_at_local']).strftime('%A %d %B at %I:%M %p')}"
            for w in resets
        ) + "."
    if re.search(r"\b(what time|current time|time is it|tell me the time)\b", utterance):
        return f"The time is {now.strftime('%I:%M %p')}."
    if re.search(r"\b(what date|today'?s date|what day is it)\b", utterance):
        return f"Today is {now.strftime('%A %d %B %Y')}."
    if "connection" in utterance or "connected" in utterance:
        return "The desktop is connected to Codex." if connected else "The desktop is not currently connected to Codex."
    return "I can report your Codex quota, reset time, date, time, or connection status."
