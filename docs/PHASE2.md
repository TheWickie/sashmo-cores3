# Phase 2 status and plan

This document distinguishes code, desktop observations and physical device tests. It does not contain local network names, credentials, account details or project paths.

## Current baseline

- The Windows dashboard, authenticated private-LAN backend, real Codex quota display and read-only push-to-talk commands are implemented.
- The CoreS3 has shown a quota details screen on the physical display, and spoken replies through its speaker have been heard in earlier device tests.
- With the DIN base switch ON and USB removed, physical tests confirmed the screen remained on, Wi-Fi pulses continued, and a voice request completed. On the revised firmware, the user heard a reply and saw a return to the main screen; the backend recorded a fresh voice request and battery-powered pulses with charging off. The user then rebooted it without USB and confirmed Wi-Fi and voice worked again. These are short tests, not a battery-runtime claim.
- The firmware now requests an immediate pulse when Wi-Fi reconnects and has configurable USB, active-battery, idle-battery and low-battery brightness. This revision compiled, flashed successfully and passed the short unplugged voice test.
- The active text response provider remains a small read-only command router. Open-ended conversation is not active.

## Milestone 1: standalone battery operation

- [x] Confirm the existing firmware can communicate and complete a voice request after USB removal.
- [x] Remove any need for an active serial connection during normal operation.
- [x] Schedule an immediate pulse when Wi-Fi reconnects.
- [x] Add configurable brightness and low-battery visual warning.
- [x] Flash the revised firmware and confirm USB and battery power transitions again.
- [x] Boot the CoreS3 with USB disconnected, then verify Wi-Fi, dashboard connection, touch, microphone and speaker.
- [x] Restart the desktop service while the CoreS3 is on battery; the dashboard restarted it and fresh Wi-Fi pulses resumed.
- [ ] Interrupt home Wi-Fi; verify offline feedback and recovery on battery.
- [ ] Record a real discharge profile in active and idle use. Do not estimate runtime from battery capacity alone.

The local service caches external Codex data independently of device pulses. USB, battery and low-battery pulse intervals are configured in the ignored local config. A stable PC address or a future verified discovery mechanism is needed; mDNS is not yet implemented.

## Next milestones

2. **CoreS3 interface:** reusable 320 × 240 visual components, clear interaction states, touch cancellation and device testing.
3. **Conversation:** official Sign in with ChatGPT OAuth for eligible local open-source apps, model discovery, account controls and multi-turn context. Keep billed inference disabled. Subscription sign-in requires user browser consent and does not expose ChatGPT history or memory.
4. **Gmail:** separate Google OAuth with read-only scopes, metadata cache, user-controlled access to message bodies and quiet notifications. Requires user account consent and Google API configuration.
5. **Codex and projects:** approved project registry, read-only status and task summaries through documented interfaces, plus explicit approval handling for consequential actions.
6. **Desktop polish:** account, conversation, device, notification and diagnostic views; reliable background service lifetime.
7. **Other services:** optional calendar, weather and reminders after their separate authorisations.
8. **Remote operation:** Android relay and Tailscale design after home battery use is robust.

## Release and security gates

- The public repository is a fresh, sanitized source snapshot. Local config, Wi-Fi password, API key, token, generated headers, binaries, machine paths and original Git history stay private.
- Public source availability alone does not establish that a specific account can use subscription-backed inference. Verify the documented OAuth scopes and actual account response before enabling it.
- Speech API use is optional and may be billed. Billed text inference is not configured or used.
- Device actions remain read-only until an explicit action-permission design and user approval are in place.
