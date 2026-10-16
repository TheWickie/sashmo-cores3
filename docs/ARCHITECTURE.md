# Architecture and protocol

## Layers

`firmware/Sashmo/Sashmo.ino` owns touchscreen UI, Wi-Fi, power status, microphone recording, speaker playback, and device timing. `backend/sashmo` owns the private LAN API, versioned state, Codex App Server client, local command router, optional ChatGPT plan text provider, optional Gmail metadata worker, and speech adapters. `desktop/service_host.py` owns and restarts the backend independently of the window; `desktop/sashmo_desktop.py` is its Tkinter control panel. Codex, ChatGPT plan text, Gmail, and the optional billed OpenAI speech endpoints have separate credentials and purposes.

## Codex state

The backend opens one persistent JSON-lines `codex app-server --stdio` process, initializes it, and requests `account/read` and `account/rateLimits/read`. It normalizes `rateLimitsByLimitId` first, with legacy `rateLimits` as fallback. It responds to `account/rateLimits/updated` by refreshing, and also refreshes every `codex_poll_seconds`. On failure it retains the last quota snapshot while flagging the Codex connection as stale and retrying with exponential backoff. No OAuth token or account secret is accessed directly.

Only actual reported usage windows are included. Durations and reset timestamps come from App Server response fields; reset time is converted to the desktop's local time zone. The firmware displays at most two windows in the Codex bucket. Reset credits and other buckets remain in the backend snapshot for future UI work.

## Device API

All routes require `X-Sashmo-Token` matching the random token in `config.local.json`. The device includes the token in firmware at build time. The HTTP service binds to the configured private IPv4 address.

- `GET /v1/pulse?since=<version>&battery=<percent>&charging=<bool>&usb=<bool>&wifi_rssi=<dBm>`: records bounded device telemetry and always returns `version`, `time`, and `date`. When `since` differs, also returns cached quota, connection state, alert count/latest alerts and Gmail unread count. Device telemetry is appended to ignored `work/battery-history.csv`; it does not trigger external polling.
- `GET /v1/status`: returns service, Codex, device, voice, Gmail metadata and alert status for the Windows dashboard. It uses the same authentication requirement and never returns a password, token or transcript.
- `GET /v1/projects`: reports only registered project display names and bounded Git summaries. Project paths remain in ignored `projects.local.json` on the PC.
- `POST /v1/voice`: request body is at most six seconds of 16 kHz mono, 16-bit PCM WAV. Response is mono PCM WAV at 24 kHz from the API or 16 kHz from local fallback. Audio is accepted only for authenticated requests and bounded in size. Transcription text and the API key are never returned to the device or written to the service log.

There is no TLS on this private LAN link. The bearer token protects access but should be considered observable by a party with network capture or physical firmware access. Do not expose the configured service port to the internet. Before an Android/Tailscale relay, remote email data or powerful Codex actions, review and upgrade transport security and action permissions.

## Voice path

On touch, the device stops its speaker, records four seconds into PSRAM, wraps the samples as WAV, and sends the recording to the desktop. The backend uses OpenAI transcription when an optional API key is configured, falling back to the installed Windows speech recognizer if unavailable. The provider handles quota/reset/time/date/connection and cached Gmail questions locally. Approved project Git status is read-only. Other questions reach the ChatGPT plan provider only after the local user completes its own OAuth sign-in and selects an account-specific model. Cedar output uses the optional speech API; Gmail metadata replies use local Windows speech so private headers are not sent to speech or text inference. Audio WAV headers are normalized before device playback. The device stops its microphone before it starts speaker playback.

No live ChatGPT plan sign-in or inference has yet been performed. The optional provider stores bounded session context in PC memory; it has no model tools or action execution. The documented Codex App Server is still used only for read-only quota reporting. No billed text inference provider is enabled.

The Sign in with ChatGPT implementation performs dynamic client registration, loopback PKCE/state/nonce, JWKS-verified ID-token checks, DPAPI-protected account storage, refresh-token rotation, account-specific model discovery and streaming Responses calls with `store=false`. Existing Codex credentials are not read or reused. Account consent and a live model response remain unverified. See https://developers.openai.com/siwc/token-sharing-open-source/sign-in and https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference.

## Service lifetime

The independent Windows host starts under the current user and has a separate Run registration for Windows sign-in. It supervises one backend process, restarts crashes with backoff, and attaches without duplicating an already running service. Dashboard control uses a random capability stored in ignored `work/service-host.json` over a loopback-only socket. Closing the dashboard does not send a stop command. An explicit Stop service action stops the host and its child. Service output and host diagnostics remain in ignored local log files.
