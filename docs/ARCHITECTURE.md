# Architecture and protocol

## Layers

`firmware/Sashmo/Sashmo.ino` owns touchscreen UI, Wi-Fi, power status, microphone recording, speaker playback, and device timing. `backend/sashmo` owns the private LAN API, versioned state, Codex App Server client, local command router, optional ChatGPT plan text provider, optional Gmail metadata and Calendar workers, and speech adapters. `desktop/service_host.py` owns and restarts the backend independently of the window; `desktop/sashmo_desktop.py` is its Tkinter control panel. Codex, ChatGPT plan text, Google grants, and the optional billed OpenAI speech endpoints have separate credentials and purposes.

## Codex state

The backend opens one persistent JSON-lines `codex app-server --stdio` process, initializes it, and requests `account/read` and `account/rateLimits/read`. It normalizes `rateLimitsByLimitId` first, with legacy `rateLimits` as fallback. It responds to `account/rateLimits/updated` by refreshing, and also refreshes every `codex_poll_seconds`. On failure it retains the last quota snapshot while flagging the Codex connection as stale and retrying with exponential backoff. No OAuth token or account secret is accessed directly.

The same process reads up to five recent task states through `thread/list` and `thread/turns/list`. The stored and displayed projection has only generic labels, states and update timestamps. It omits task titles, prompts, paths, IDs and turn content. A task-list failure does not interrupt quota refresh.

Only actual reported usage windows are included. Durations and reset timestamps come from App Server response fields; reset time is converted to the desktop's local time zone. The firmware displays at most two windows in the Codex bucket. Reset credits and other buckets remain in the backend snapshot for future UI work.

## Device API

All routes require `X-Sashmo-Token` matching the random token in `config.local.json`. The device includes the token in firmware at build time. The HTTP service binds to the configured private IPv4 address.

- `GET /v1/pulse?since=<version>&battery=<percent>&charging=<bool>&usb=<bool>&wifi_rssi=<dBm>`: records bounded device telemetry and always returns `version`, `time`, and `date`. When `since` differs, also returns cached quota, connection state, alert count/latest alerts, Gmail unread count and Calendar event count. Device telemetry is appended to ignored `work/battery-history.csv`; it does not trigger external polling.
- `GET /v1/status`: returns service, Codex, device, voice, Gmail metadata, Calendar events and alert status for the Windows dashboard. It requires the token and a connection originating from the backend PC. It never returns a password, token or transcript.
- `GET /v1/projects`: reports only registered project display names and bounded Git summaries. Project paths remain in ignored `projects.local.json` on the PC.
- `POST /v1/voice`: request body is at most six seconds of 16 kHz mono, 16-bit PCM WAV. Response is mono PCM WAV at 24 kHz from the API or 16 kHz from local fallback. Audio is accepted only for authenticated requests and bounded in size. Transcription text and the API key are never returned to the device or written to the service log.
- A follow-up `/v1/voice` request carries `X-Sashmo-Follow-Up: 1`. If it contains no recognized speech, the backend returns `204` with no audio. The CoreS3 also gates silent follow-up clips before upload.
- `POST /v1/wake`: checks at most three seconds of authenticated PCM WAV with Windows' offline phrase grammar. It returns only a boolean; wake clips are never sent to OpenAI or logged.
- `POST /v1/codex-task/approve` and `/reject`: desktop-origin-only final review of a voice-drafted coding request. Only the approved Git root is writable, and the Codex task has network access disabled.

There is no TLS on this private LAN link. The bearer token protects access but should be considered observable by a party with network capture or physical firmware access. The local source-address gate on dashboard endpoints is not a substitute for encrypted transport. Do not expose the configured service port to the internet. See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for the required controls before an Android/Tailscale relay, remote email data or powerful Codex actions.

## Voice path

On touch or after a recognized wake phrase, the device stops its speaker, records six seconds into PSRAM, wraps the samples as WAV, and sends the recording to the desktop. After each spoken reply, it records another six-second follow-up, up to eight turns in one exchange; silence ends the exchange. The backend uses OpenAI transcription when an optional API key is configured, falling back to the installed Windows speech recognizer if unavailable. The provider handles quota/reset/time/date/connection and cached Gmail and Calendar questions locally. Approved project Git status is read-only. Other questions reach the ChatGPT plan provider only after the local user completes its own OAuth sign-in and selects an account-specific model. Cedar output uses the optional speech API; Gmail metadata and Calendar replies use local Windows speech so private content is not sent to speech or text inference. Audio WAV headers are normalized before device playback. The device stops its microphone before it starts speaker playback.

Calendar uses a separate Google OAuth grant from Gmail, even when both share one Desktop OAuth client ID. Its worker polls independently of the CoreS3, requests only primary-calendar event summary/start/end/visibility fields in a bounded 48-hour window, and caches the result in memory. Private event titles are replaced by a generic label. The firmware receives a count; the Windows dashboard receives the cached event list over the authenticated private-LAN API. A transport-security upgrade is required before exposing this data through a remote relay.

A live ChatGPT plan sign-in and a text reply have been verified on the desktop. The optional provider stores bounded session context in PC memory; it has no model tools or action execution. The documented Codex App Server supplies quota, recent task/project summaries, and desktop-approved coding task dispatch. No billed text inference provider is enabled.

The Sign in with ChatGPT implementation performs dynamic client registration, loopback PKCE/state/nonce, JWKS-verified ID-token checks, DPAPI-protected account storage, refresh-token rotation, account-specific model discovery and streaming Responses calls with `store=false`. Existing Codex credentials are not read or reused. Account consent and a live text response have been verified; physical multi-turn voice use still needs testing. See https://developers.openai.com/siwc/token-sharing-open-source/sign-in and https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference.

## Service lifetime

The independent Windows host starts under the current user and has a separate Run registration for Windows sign-in. It supervises one backend process, restarts crashes with backoff, and attaches without duplicating an already running service. Dashboard control uses a random capability stored in ignored `work/service-host.json` over a loopback-only socket. Closing the dashboard does not send a stop command. An explicit Stop service action stops the host and its child. Service output and host diagnostics remain in ignored local log files.
