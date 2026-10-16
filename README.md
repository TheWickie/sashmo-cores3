# Sashmo

Sashmo is an open-source voice and status companion for the M5Stack CoreS3 and a Windows PC. The CoreS3 provides the touchscreen, microphone, speaker and battery-powered Wi-Fi connection. A local Windows service retrieves reported Codex usage and handles read-only voice questions; a desktop dashboard starts the service and shows device status.

This is an active hardware project. Push-to-talk, quota display, battery telemetry and the dashboard work. Natural conversation, Gmail and other personal integrations are planned, not yet active. See [Phase 2 status](docs/PHASE2.md) for verified behaviour and release blockers.

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

Create `config.local.json` by running `python backend/setup_config.py --host <YOUR_PC_PRIVATE_IP>`. The script asks for the Wi-Fi network name without echoing it. Put the Wi-Fi password as one line in `wifipassword.txt` at the repository root. These files are ignored by Git. `config.example.json` shows the available settings; edit your private config to adjust the service port, pulse intervals and display brightness.

To use OpenAI transcription and Cedar speech, optionally put an OpenAI API key as one line in `openAIAPIKey.txt`. These audio API calls may incur charges. Without a key, Sashmo uses installed Windows speech fallbacks. Conversational inference is not enabled and is never silently routed to a separately billed API.

The service binds only to the private IPv4 address configured in `config.local.json`, and every device request carries a random device token. The current LAN link uses HTTP, so use it only on a trusted local network; do not forward the service port to the internet. The OpenAI key and Codex sign-in stay on the PC. The CoreS3 firmware contains only its Wi-Fi credentials and device token.

## Build, flash and run

From the repository root, with the CoreS3 connected by USB:

```powershell
powershell -ExecutionPolicy Bypass -File .\flash_sashmo.ps1 -BuildOnly
powershell -ExecutionPolicy Bypass -File .\flash_sashmo.ps1 -Port COM4
```

Change `COM4` to the detected device port. The script creates a private firmware header, builds or uploads, then removes the header. Reflash when the Wi-Fi name, password, device token, server address or firmware power settings change.

Double-click `Sashmo.cmd` to open the Windows dashboard. It starts and monitors the local service. The Overview shows reported Codex quota, the last CoreS3 check-in, battery, charging, Wi-Fi signal and voice readiness. Settings includes the Codex refresh interval and optional launch at Windows sign-in. Diagnostics shows service output without credentials or transcripts. Closing the dashboard stops a service that it started.

Tap the microphone control on the CoreS3 and speak within the listening window. The current read-only questions cover quota, reset time, time, date and connection status. Tap the quota card for details and tap again to return. Voice and status use Wi-Fi; USB is needed for charging, flashing and optional serial diagnostics only.

If the PC's LAN address or service port changes, update `config.local.json` and reflash the CoreS3. Reserving the PC's address in the home router is a practical way to keep the current connection stable. Hostname or mDNS discovery is a future improvement.

## Development

Run backend tests from `backend` with `python -m pytest -q`. `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` and `docs/PHASE2.md` describe the design and current limits. Keep private files and compiled firmware images out of commits and issue reports.
