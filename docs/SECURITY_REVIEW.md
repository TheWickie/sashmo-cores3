# Private LAN security review

This review covers the current CoreS3-to-Windows link. It does not approve Android, Tailscale, internet exposure, email actions, or voice-driven Codex changes.

## Current boundary

- The backend binds to the configured private IPv4 address and requires one random device token in `X-Sashmo-Token` on every endpoint.
- The CoreS3 stores that token and its Wi-Fi password in firmware. ChatGPT, Google, Codex and optional speech credentials stay on Windows.
- `/v1/pulse` sends status counts and quota data to the device. `/v1/voice` accepts bounded WAV input and returns WAV audio. These endpoints remain reachable from the trusted home LAN with the device token.
- `/v1/status` and `/v1/projects` return richer dashboard data, including cached Gmail headers, Calendar event titles, approved project summaries and recent task states. They now require both the token and a connection originating from the backend PC's own configured address. The dashboard and service host run on that PC.
- The transport is HTTP. The token and voice audio can be observed or replayed by a party able to capture or alter home-LAN traffic. Anyone who extracts the token from the device can call the device-facing endpoints. Source-address checks are an additional boundary for dashboard data, not cryptographic authentication.
- A voice reply can include a cached mail header or Calendar title. It is generated on Windows and returned as unencrypted WAV to the device. This remains a privacy exposure on an untrusted LAN even though the speech service is local.

## Current operational rule

Use Sashmo only on a trusted private LAN. Do not forward its port, bind to a public address, place it behind a public tunnel, or reuse the device token for an Android client. Keep voice capabilities read-only. The backend rejects unauthorized requests before recording device telemetry or processing audio.

The iPad Command Centre adds a different, read-only browser credential. Pairing requires an eight-digit rotating code visible on the Windows dashboard. Requests must come from the configured home `/24`, and the signed cookie is HttpOnly, SameSite Strict and scoped to `/ipad`. Browser write requests check JSON content type and same-origin headers. Five failed code attempts per address pause pairing for 15 minutes. The cookie remains valid for 30 days and is signed from the existing desktop secret, so changing the device token invalidates it. This protects against casual unauthenticated LAN access, but **HTTP still exposes the pairing exchange, cookie and displayed private data to local network capture or alteration**. Do not use the iPad page on guest Wi-Fi or through a tunnel. The subnet gate is an operational boundary, not proof of identity. HTTPS with a trusted certificate and separate revocable credentials is required before remote use or background push.

## Gate before remote access or powerful actions

1. Add encrypted transport with server identity verification for the CoreS3 and any remote client. Decide whether the embedded device will pin a server certificate or use another maintainable trust anchor, and test renewal before deployment.
2. Use separate, scoped credentials for the CoreS3, local dashboard and remote clients. Support rotation and revocation after device loss. The current shared device token is insufficient for different privilege levels.
3. Keep a privileged control surface local to Windows. Require explicit user confirmation for email sending, project changes, Codex actions, approvals, purchases and other consequential operations. Do not infer consent from a spoken command alone.
4. Minimize data returned to the device. Preserve count-only idle updates and send event or message details only for a user-requested interaction. Define a privacy mode for spoken private data.
5. Test replay, unauthorized access, device loss, Wi-Fi changes, expired certificates/tokens and backend recovery. Review logs for credential and transcript leakage.

Until those controls are implemented and tested, Android/Tailscale integration and powerful voice actions remain disabled.
