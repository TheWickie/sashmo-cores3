# Sashmo Deck

Optional Windows command centre for the Elgato 15-key Stream Deck 2019 (USB
`0FD9:006D`). The CoreS3 and iPad continue working without it. The Deck app
reads the independent Sashmo backend's cached state; it never polls Gmail,
Calendar, ChatGPT or Codex itself.

## Start and configure

1. Close the Elgato Stream Deck application so that it cannot also execute key
   actions. Sashmo never terminates it automatically.
2. Double-click `Deck.cmd`, or open **Settings → Open Sashmo Deck** in the
   Windows dashboard. First launch creates an ignored `.venv` and installs
   `hidapi` and Pillow. The script also asks the independent service host to
   start, then opens the Deck window.
3. In the Deck window, choose **Settings**. Set two existing project folders,
   optionally register up to three saved ChatGPT conversation URLs, and adjust
   brightness or hide personal labels. Settings are stored in the ignored
   `deck.local.json`. `deck.example.json` shows the format.

If the Elgato app remains open, the Sashmo preview works but USB control is
disabled. USB control resumes after Elgato closes, without changing its profiles.
The Deck window can close
without stopping the independent backend.

## Keys

| Row | Key 1 | Key 2 | Key 3 | Key 4 | Key 5 |
| --- | --- | --- | --- | --- | --- |
| Top | Project 1 | Project 2 | Gmail | Saved Chat 1 | Recent VS Code Codex 1 |
| Middle | Codex tasks | Reported weekly quota | Calendar | Saved Chat 2 | Recent VS Code Codex 2 |
| Bottom | Alerts | CoreS3 status | Talk | Saved Chat 3 | Recent VS Code Codex 3 |

Project keys open configured folders. Gmail and Calendar open their normal
websites. Saved chat keys open registered conversation links. The three Codex
keys use read-only `thread/list` with `sourceKinds: ["vscode"]`, then open the
documented `codex://threads/<thread-id>` link. Empty slots say so. The weekly
key shows a percentage only when Codex reports a 10,080-minute window; it
does not infer one. Status keys reflect the last backend snapshot, and an
unavailable integration says **Unavailable**.

ChatGPT does not provide an authoritative account-wide recent-chat list to
this integration. Saved chat links are explicit local registrations, not a
claim of complete or automatically refreshed history.

The Talk key posts to a token-authenticated endpoint available only from the
backend PC. The service queues one short-lived command with a randomised
sequence, and the CoreS3 receives it in its next authenticated pulse. It
deduplicates the command across retries, reports started/busy, wakes its
screen, then uses the same microphone, transcription, conversation and speaker
path as its touchscreen microphone. The Deck does not record audio itself.
While the Deck window polls status, CoreS3 pulses speed up to four seconds on
USB or twelve seconds on normal battery power after the next regular pulse.
Low battery keeps its power-saving interval and the Talk key is rejected.

All Deck status and Talk endpoints require the existing device token **and** a
request from the configured backend PC address. The device link is still
authenticated HTTP on the private LAN, with the limitations in
`docs/SECURITY_REVIEW.md`. Do not forward the backend port to the internet.

## Verification

From `backend`, run `python -m pytest -q`. From the repo root, run
`python -m pytest -q deck/tests`. `flash_sashmo.ps1 -BuildOnly` checks the
firmware without uploading it. Hardware verification also requires closing
the Elgato application, checking all 15 keys on the actual Deck, pressing
Talk with the CoreS3 on Wi-Fi, checking voice playback, and testing USB
reconnect. Never treat the preview or a successful build as that physical
verification.
