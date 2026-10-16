# Phase 2 status and plan

This document distinguishes code, desktop observations and physical device tests. It does not contain local network names, credentials, account details or project paths.

## Current baseline

- The Windows dashboard, authenticated private-LAN backend, real Codex quota display and read-only push-to-talk commands are implemented.
- The CoreS3 has shown a quota details screen on the physical display, and spoken replies through its speaker have been heard in earlier device tests.
- With the DIN base switch ON and USB removed, physical tests confirmed the screen remained on, Wi-Fi pulses continued, and a voice request completed. On the revised firmware, the user heard a reply and saw a return to the main screen; the backend recorded a fresh voice request and battery-powered pulses with charging off. The user then rebooted it without USB and confirmed Wi-Fi and voice worked again. These are short tests, not a battery-runtime claim.
- The firmware now requests an immediate pulse when Wi-Fi reconnects and has configurable USB, active-battery, idle-battery and low-battery brightness. This revision compiled, flashed successfully and passed the short unplugged voice test.
- An independent per-user Windows service host now owns the backend. In a live migration, the old dashboard closed, the host took over, the CoreS3 resumed pulses, and the host restarted the backend after a forced process exit. The host is registered for Windows sign-in; closing the dashboard no longer stops the backend.
- The active text response provider remains local until a user signs in through Sashmo and explicitly selects an available ChatGPT model. The subscription-backed path is implemented but has not been live-authorized or tested on this account.

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

The desktop service now appends authenticated device pulses to the ignored `work/battery-history.csv` with UTC time, charge percentage, charging, USB power and Wi-Fi signal. This prepares a measured discharge test; it does not establish runtime while the device remains plugged in.

The local service caches external Codex data independently of device pulses. USB, battery and low-battery pulse intervals are configured in the ignored local config. A stable PC address or a future verified discovery mechanism is needed; mDNS is not yet implemented.

## Next milestones

2. **CoreS3 interface:** reusable 320 × 240 visual components, clear interaction states, touch cancellation and device testing.
3. **Conversation:** the official Sign in with ChatGPT OAuth flow, protected local account storage, model discovery and bounded multi-turn text context are implemented. A user must complete browser consent and select a listed model before natural voice replies are enabled. No live sign-in or inference has run. Subscription sign-in does not expose ChatGPT history or memory; billed text API inference remains disabled.
4. **Gmail:** separate Google OAuth with read-only scopes, metadata cache, user-controlled access to message bodies and quiet notifications. Requires user account consent and Google API configuration.
   **Calendar:** a separate read-only Google grant, bounded upcoming-event cache and local voice answers are implemented. Live browser consent and event verification remain outstanding.
5. **Codex and projects:** approved project registry, read-only status and task summaries through documented interfaces, plus explicit approval handling for consequential actions.
6. **Desktop polish:** account, conversation, device, notification and diagnostic views; reliable background service lifetime.
7. **Other services:** optional calendar, weather and reminders after their separate authorisations.
8. **Remote operation:** Android relay and Tailscale design after home battery use is robust.

## Release and security gates

- The public repository is a fresh, sanitized source snapshot. Local config, Wi-Fi password, API key, token, generated headers, binaries, machine paths and original Git history stay private.
- Public source availability alone does not establish that a specific account can use subscription-backed inference. Verify the documented OAuth scopes and actual account response before enabling it.
- Speech API use is optional and may be billed. Billed text inference is not configured or used.
- Device actions remain read-only until an explicit action-permission design and user approval are in place.
- The device link is authenticated HTTP on the trusted private LAN. Android/Tailscale access, remote email exposure and powerful Codex actions require a transport-security and permission review before enabling them.

## ChatGPT plan conversation

The Windows dashboard has a ChatGPT page with a system-browser sign-in button, saved account selection, current model discovery and an explicit model enable action. Credentials are protected for the current Windows user with DPAPI in LocalAppData. The CoreS3 never receives them. The text provider uses only an account's authorized ChatGPT plan token, requires a model reported by that account, and sends Responses requests with `store=false` and `stream=true`. Unknown voice questions use this provider only after the user signs in and selects a model; quota, reset, time and connection answers stay local. A plan error gives an error reply and never switches to the speech API key for text inference. Conversation context is held in desktop memory for up to 30 minutes and is not durable memory. The browser consent, model list and a real conversational reply still need live verification.

## Gmail and alerts

The optional Gmail connector uses a separate Google Desktop OAuth flow and requests only `gmail.metadata`. It can cache an unread count and up to ten recent message headers; it cannot read bodies or use Gmail search with this scope. Metadata polling is independent of CoreS3 pulses. The desktop page displays the cached headers. A voice request for email uses only the cached snapshot and local Windows speech, so message metadata is not sent to Cedar or the ChatGPT text provider. Browser consent and a live metadata request succeeded with the local test account. This Desktop OAuth client required its client secret at Google's token endpoint despite the generic desktop guide describing it as optional; the secret is kept in ignored local config and the Windows-protected grant. Public distribution may still require Google's restricted-scope verification. The alert store currently covers low battery only. The alert UI firmware compiled, flashed with verified hash, and reconnected via Wi-Fi; the new alert screen itself has not yet been checked by touch on the physical display.

## Calendar

The Calendar page can use the same Google Desktop OAuth client after the Calendar API is enabled, but asks for its own `calendar.events.readonly` grant. The backend refreshes up to 20 events from the primary calendar in the next 48 hours without depending on device pulses. It requests only summary, visibility and start/end fields; private titles are masked. Spoken answers use the cached snapshot and local Windows speech. The CoreS3 receives only an event count. Browser consent and a live upcoming-events request succeeded with the local test account. Spoken Calendar answers and the event count on the physical CoreS3 still need direct verification.

The CoreS3 firmware now renders connected Gmail/Calendar counts on the idle screen, omitting them when disconnected. This build compiled, flashed to COM4 with hash verification, and resumed authenticated Wi-Fi pulses after reboot. The new count layout has not yet been visually checked on the physical screen.

## Approved project registry

The dashboard's Projects page lets the local user choose Git repository folders. The private `projects.local.json` allowlist is ignored by Git. The authenticated service reports only the approved display name, branch, clean/dirty state and last commit identifier/time; it does not send paths or source contents to the device. No project is inspected until it is explicitly added. The Overview can count recent Codex task states through read-only App Server methods, using generic labels and no task content. A local voice answer reports active/completed counts among the five recent tasks; detailed task summaries remain future work.
