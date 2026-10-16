# Roadmap

## Working foundation

- CoreS3 touchscreen, Wi-Fi connection, authenticated LAN pulse, microphone recording and speaker playback.
- Windows service with cached reported Codex quota and optional OpenAI transcription/Cedar speech.
- Windows dashboard for quota, device telemetry, service controls and diagnostics.
- Read-only voice commands for quota, reset time, clock, date and connection status.
- Short physical battery and battery-only boot tests: screen, Wi-Fi and voice worked without USB. Longer runtime is still unverified.

## Phase 2 sequence

1. Standalone battery and home Wi-Fi reliability, including a measured runtime profile.
2. CoreS3 interface redesign and interaction testing.
3. Subscription-backed conversation through documented Sign in with ChatGPT, subject to actual OAuth consent and eligibility.
4. Optional read-only Gmail integration with separate Google consent.
5. Codex status and approved project registry with explicit action permissions.
6. Windows dashboard, notification and account-management polish.
7. Optional calendar, weather and reminders.
8. Secure portable relay through Android and Tailscale.

See [Phase 2 status](PHASE2.md) for current acceptance checks and dependencies. No future integration is represented as working until tested against its real service and, where relevant, the physical CoreS3.
