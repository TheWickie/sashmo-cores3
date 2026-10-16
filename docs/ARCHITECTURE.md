# Architecture and protocol

## Layers

`firmware/Sashmo/Sashmo.ino` owns touchscreen UI, Wi-Fi, power status, microphone recording, speaker playback, and device timing. `backend/sashmo` owns the private LAN API, versioned state, Codex App Server client, bounded command router, and speech adapters. `desktop/sashmo_desktop.py` is a local Tkinter dashboard and service launcher. The Codex App Server and OpenAI speech endpoints are separate integrations. There is no subscription-backed audio API path.

## Codex state

The backend opens one persistent JSON-lines `codex app-server --stdio` process, initializes it, and requests `account/read` and `account/rateLimits/read`. It normalizes `rateLimitsByLimitId` first, with legacy `rateLimits` as fallback. It responds to `account/rateLimits/updated` by refreshing, and also refreshes every `codex_poll_seconds`. On failure it retains the last quota snapshot while flagging the Codex connection as stale and retrying with exponential backoff. No OAuth token or account secret is accessed directly.

Only actual reported usage windows are included. Durations and reset timestamps come from App Server response fields; reset time is converted to the desktop's local time zone. The firmware displays at most two windows in the Codex bucket. Reset credits and other buckets remain in the backend snapshot for future UI work.

## Device API

All routes require `X-Sashmo-Token` matching the random token in `config.local.json`. The device includes the token in firmware at build time. The HTTP service binds to the configured private IPv4 address.

- `GET /v1/pulse?since=<version>&battery=<percent>&charging=<bool>&usb=<bool>&wifi_rssi=<dBm>`: records bounded device telemetry and always returns `version`, `time`, and `date`. When `since` differs, also returns `quota`, `codex_connected`, `error`, and `last_updated_local`. Quota fields are absent from an unchanged pulse. Telemetry does not bump the quota version.
- `GET /v1/status`: returns the service, Codex, device and voice status for the Windows dashboard. It uses the same authentication requirement and never returns a password, token or transcript.
- `POST /v1/voice`: request body is at most six seconds of 16 kHz mono, 16-bit PCM WAV. Response is mono PCM WAV at 24 kHz from the API or 16 kHz from local fallback. Audio is accepted only for authenticated requests and bounded in size. Transcription text and the API key are never returned to the device or written to the service log.

There is no TLS on this private LAN link. The bearer token protects access but should be considered observable by a party with network capture or physical firmware access. Do not expose the configured service port to the internet. An encrypted relay is a future milestone.

## Voice path

On touch, the device stops its speaker, records four seconds into PSRAM, wraps the samples as WAV, and sends the recording to the desktop. The backend uses OpenAI transcription when an optional API key is configured, falling back to the installed Windows speech recognizer if unavailable. A replaceable `TextResponseProvider` currently runs the bounded read-only quota/reset/time/date/connection parser; a later conversational provider can implement the same interface without changing audio transport. Cedar voice output uses the OpenAI speech endpoint when configured, with Windows System.Speech as fallback. API streaming WAV headers are normalized to exact lengths for device playback. The device stops its microphone before it starts speaker playback.

Conversational inference is not active. The documented Codex App Server can support authenticated Codex interaction, but this v0.1 service intentionally exposes only the read-only quota path. No billed inference provider is enabled.

Current OpenAI documentation describes a separate Sign in with ChatGPT plan-usage flow for locally hosted open-source apps. It requires Sashmo to register an OAuth client, obtain the user's consent, store and refresh its own tokens, and send supported text requests through the Responses API or a separately configured Codex App Server. Existing Codex sign-in credentials are not read or reused. Sashmo has not implemented this flow; no conversational inference request is made. See https://developers.openai.com/siwc/token-sharing-open-source and https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server.
