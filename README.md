# Sashmo


Sashmo is an open-source voice and status companion for the M5Stack CoreS3 and a Windows PC. The CoreS3 provides the touchscreen, microphone, speaker and battery-powered Wi-Fi connection. A local Windows service retrieves reported Codex usage and handles read-only voice questions; a desktop dashboard starts the service and shows device status.

The Labrador illustration in `assets/sashmo-labrador.png` is the project mascot. A small JPEG version appears briefly when the CoreS3 starts.

This is an active hardware project. Push-to-talk, quota display, battery telemetry and the dashboard work. A local ChatGPT plan sign-in and one text response succeeded; CoreS3 follow-up conversation still needs a physical test. Gmail metadata and Calendar upcoming-event connections have completed live read-only requests. See [Phase 2 status](docs/PHASE2.md) for verified behaviour and release blockers.

## Easy mode: set up with Codex

On a Windows PC, plug in a CoreS3, set its DIN base switch to **ON**, then paste this prompt into your own Codex task:

```text
Set up Sashmo from https://github.com/TheWickie/sashmo-cores3 on this Windows PC for my M5Stack CoreS3. Read README.md and AGENTS.md first. Clone into a folder I choose (default: Documents\Sashmo), or continue an existing checkout without overwriting my changes.

Install the documented Python and Arduino CLI dependencies. Find this PC's private LAN IPv4 address and the CoreS3's actual USB serial port; do not assume COM4. Create the ignored local configuration with backend/setup_config.py. Help me enter my Wi-Fi name and password into the private local files without printing either one in chat, command output, logs, or Git. Keep the generated device token private too.

Run the backend tests, build the firmware, flash only the identified CoreS3, and start the independent Windows backend and dashboard. Check that the device connects over Wi-Fi and reports a recent pulse, then guide me through a microphone and speaker test. Tell me what you verified and what still needs my physical input.

Leave paid OpenAI speech, ChatGPT plan conversation, Gmail, and Calendar disconnected unless I separately choose to set them up and complete their sign-in or billing steps. Do not enable billed text inference, expose the backend to the internet, or commit any credential or generated firmware file.
```

Codex will still need your help to enter Wi-Fi credentials locally, identify the connected device, and confirm the physical voice test. The manual steps below remain available if you prefer to set it up yourself.

## Requirements

- An M5Stack CoreS3 with DIN base and battery. Set the DIN base power switch to **ON**.
- Windows with Python 3.12 or newer and the Codex CLI signed in for quota reporting.
- Arduino CLI with the M5Stack board index in `arduino-cli.yaml`, M5Stack ESP32 core 3.3.10, M5Unified 0.2.26, M5GFX 0.2.33 and ArduinoJson 7.4.3. The flashing scripts use `tools/arduino-cli.exe` when present, or `arduino-cli.exe` on `PATH`.
- A private home Wi-Fi network shared by the CoreS3 and PC.

Install the desktop dependencies from the `backend` directory:

```powershell
python -m pip install -r requirements.txt
```

For a fresh Arduino CLI installation, from the repository root:

```powershell
arduino-cli --config-file arduino-cli.yaml core update-index
arduino-cli --config-file arduino-cli.yaml core install m5stack:esp32@3.3.10
arduino-cli --config-file arduino-cli.yaml lib install ArduinoJson@7.4.3 M5GFX@0.2.33 M5Unified@0.2.26
```

## Private setup

Create `config.local.json` by running `python backend/setup_config.py --host <YOUR_PC_PRIVATE_IP>`. The script asks for the Wi-Fi network name without echoing it. Put the Wi-Fi password as one line in `wifipassword.txt` at the repository root. These files are ignored by Git. `config.example.json` shows the available settings; edit your private config to adjust the service port, pulse intervals, display brightness and `screen_standby_after_seconds`. The display enters standby after five minutes of inactivity by default, on USB or battery. Wi-Fi keeps running; the first tap wakes the display without activating a control. Changing this setting requires reflashing the device.

To use OpenAI transcription and Cedar speech, optionally put an OpenAI API key as one line in `openAIAPIKey.txt`. These audio API calls may incur charges. Without a key, Sashmo uses installed Windows speech fallbacks. Conversational inference is not enabled and is never silently routed to a separately billed API.

For optional natural conversation, open the dashboard's ChatGPT page, choose **Continue with ChatGPT**, complete the system-browser consent, then choose a model listed for that account and select **Use selected model**. This uses a separate local OAuth registration and ChatGPT plan permission. It does not reuse the Codex login or the speech API key. A private integration test completed sign-in and one text reply; eligibility and available models depend on each account. If sign-in or plan use fails, Sashmo keeps local read-only voice commands and never uses billed text API inference as a fallback. The Projects page has **Discussion access**: you can write notes for an approved project and optionally allow read-only excerpts from tracked text files. When you name that project in a conversational question, up to five relevant files (12,000 characters total) and bounded Git status may be supplied to the selected ChatGPT model. File reading is off by default for newly added projects. Sashmo skips untracked files, symlinks outside the project, private-looking filenames and text containing common credential patterns; review a repository before enabling it, as no automatic filter can guarantee every secret is recognized.

The service binds only to the private IPv4 address configured in `config.local.json`, and every device request carries a random device token. The current LAN link uses HTTP, so use it only on a trusted local network; do not forward the service port to the internet. The OpenAI key and Codex sign-in stay on the PC. The CoreS3 firmware contains only its Wi-Fi credentials and device token.

## Build, flash and run

From the repository root, with the CoreS3 connected by USB:

```powershell
powershell -ExecutionPolicy Bypass -File .\flash_sashmo.ps1 -BuildOnly
powershell -ExecutionPolicy Bypass -File .\flash_sashmo.ps1 -Port COM4
```

Change `COM4` to the detected device port. The script creates a private firmware header, builds or uploads, then removes the header. Reflash when the Wi-Fi name, password, device token, server address or firmware power settings change.

Double-click `Sashmo.cmd` to open the Windows dashboard. It starts or connects to an independent local service host. The Overview shows reported Codex quota, the last CoreS3 check-in, battery, charging, Wi-Fi signal and voice readiness. Settings includes the Codex refresh interval and backend startup at Windows sign-in. Diagnostics shows service output without credentials or transcripts. **Closing the dashboard leaves the backend running.** The host restarts a crashed backend; use the dashboard's Stop service control when you explicitly want to stop it.

The Overview also shows a count of recent Codex task states from the documented App Server read methods. These generic summaries contain no task title, prompt, working directory or project path.

The Projects page remembers the ten most recently active distinct Codex project folders from local task history. It shows folder names only, skips temporary Codex chat folders, and never reads their source files. Ask “What are my recent projects?” for a local spoken list. This list is separate from the approved Git projects below it: seeing a project in recent history does not authorize Sashmo to inspect or change it.

To draft a coding task, say “In project Sample, add a dashboard for tasks” using an approved Git project name. Sashmo reads the request back. Say “confirm Codex request” within two minutes, then review the exact transcript in the Windows Projects page and select **Send to Codex**. The task starts in that approved project with workspace write access and no network access; its result appears as a Codex task. This final desktop review is required while device traffic uses authenticated HTTP on the private LAN. Voice alone cannot launch a coding task, and task approval is unavailable from the device-facing API.
You can ask “What's Codex doing?” for a spoken count of active and completed tasks among the five recent tasks Sashmo can see. Open Codex itself for task details.

Use the Projects page to add Git repositories that Sashmo may inspect. Its read-only status view shows branch, uncommitted changes and last commit. Approved paths stay in the ignored `projects.local.json`; no project is inspected before you add it.

The Gmail page supports a separate Google Desktop OAuth client ID and browser consent. Enter the Desktop client secret in the masked field if Google's token endpoint requires it; it is saved only in ignored `config.local.json` and the Windows-protected account store. The current connector requests Gmail metadata only and shows recent sender, subject, date and unread count. It does not read bodies, search, send or modify mail. Google classifies this scope as restricted; public distribution may require Google verification. Gmail remains disconnected until you complete consent.

The Calendar page uses the same Google Desktop OAuth client ID and secret, with its own browser consent separate from Gmail. Enable the Google Calendar API in the same Google Cloud project, then enter the client credentials and select **Connect Calendar**. Sashmo requests `calendar.events.readonly` and caches up to 20 upcoming events from the primary calendar for the next 48 hours. It requests no descriptions, locations, attendees or conference details; private event titles are masked. Event data stays on the PC and is spoken with local Windows speech. The CoreS3 receives only an event count. Calendar remains disconnected until browser consent succeeds.

While the Google OAuth app is **External / Testing**, Google expires its refresh tokens after seven days for these scopes. Reconnect Calendar and Gmail in the dashboard when prompted. [Google OAuth token policy](https://developers.google.com/identity/protocols/oauth2)

When Gmail or Calendar is connected, the CoreS3 idle screen shows compact unread-mail and upcoming-event counts. Disconnected services do not display a zero count.
If a Google refresh fails, the dashboard marks cached information as potentially old, the device hides the affected count, and voice stops reporting it as current until the connection recovers.

Tap the microphone control on the CoreS3 and start speaking within six seconds. Sashmo waits for a pause after speech before Processing, with a 20-second maximum recording length. After each spoken reply, it listens for another question, for up to eight turns total. Silence ends the exchange without a spoken error. Tapping the microphone area while the screen is asleep wakes it and starts listening. Local answers cover quota, reset time, time, date, connection status and recent Codex projects; other questions can use the selected ChatGPT plan model. A project coding request becomes a draft for desktop review. Tap the quota card for details and tap again to return. Voice and status use Wi-Fi; USB is needed for charging, flashing and optional serial diagnostics only.

Tap **VOL** on the idle screen for large − and + controls in 10 percent steps. The setting survives a reboot and also controls the wake beep. You can say **“set volume to 40 percent”** during a voice turn; Sashmo applies the requested level before speaking its confirmation. Values from 0 to 100 percent are accepted, including spoken number words.

Hands-free wake is optional and off in a fresh installation. Set `wake_word_enabled` to `true` in the ignored `config.local.json` and reflash the CoreS3. It listens for **“Hello Sashmo”** while USB powered; set `wake_on_battery` to `true` and reflash if you accept shorter battery life. After the wake beep, start your request while Listening appears. Continue speaking when the next Listening screen appears after Sashmo's reply; you do not need to repeat the wake phrase during that exchange. Wake clips are checked by Windows speech recognition on the desktop and are never sent to OpenAI; the request after waking follows the normal transcription and reply pipeline. Candidate wake clips travel over authenticated HTTP on the private LAN, which is not encrypted. The display can remain in standby while wake listening runs.

If the PC's LAN address or service port changes, update `config.local.json` and reflash the CoreS3. Reserving the PC's address in the home router is a practical way to keep the current connection stable. Hostname or mDNS discovery is a future improvement.

The CoreS3 link currently uses authenticated HTTP on a trusted private LAN. Rich dashboard status is restricted to connections from the backend PC, but the device token and voice audio still travel over HTTP. Before Android/Tailscale access or more powerful Codex actions, the transport and permission design must be upgraded and tested. Do not expose the local service port to the internet. See the [private LAN security review](docs/SECURITY_REVIEW.md).

## Development

Run backend tests from `backend` with `python -m pytest -q`. `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` and `docs/PHASE2.md` describe the design and current limits. Keep private files and compiled firmware images out of commits and issue reports.
