# Sashmo engineering notes

- Work in this repository for CoreS3 firmware and the Windows service. Keep firmware, backend, and integration code separate.
- Do not print, commit, or copy `wifipassword.txt`, `openAIAPIKey.txt`, `config.local.json`, generated private headers, or compiled firmware images. The generated header is removed by `flash_sashmo.ps1` after each build.
- The device may hold only its Wi-Fi password and a Sashmo device token. The OpenAI API key and Codex credentials remain on the desktop.
- Read Codex limits from the documented App Server. Show only reported windows; never infer a five-hour window or translate the reported plan code into a different tier.
- Device pulses must read cached state. Keep external Codex polling independent of pulse frequency.
- Voice commands in v0.1 are read-only. Do not add filesystem, shell, reset-credit, purchase, or publication actions through voice without an explicit new requirement.
- An optional API key may be used for speech-to-text and Cedar text-to-speech only. Do not route conversational inference through a billed API without the local user's separate authorization.
- When hardware cannot be observed directly, mark it unverified and record the concrete serial or user-observed evidence separately.
- Build: `powershell -ExecutionPolicy Bypass -File .\flash_sashmo.ps1 -BuildOnly`; flash: omit `-BuildOnly`; backend tests: `cd backend; python -m pytest -q`.
