"""Create the local desktop configuration without printing its device secret."""

import argparse
import getpass
import json
import secrets
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="This PC's private LAN IPv4 address")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "config.local.json"
    if path.exists():
        print(f"Configuration already exists: {path}")
        return
    wifi_ssid = getpass.getpass("Wi-Fi network name (hidden): ").strip()
    if not wifi_ssid or any(char in wifi_ssid for char in "\r\n\0"):
        raise SystemExit("Wi-Fi network name must be one nonempty line")
    data = {
        "bind_host": args.host,
        "port": 8765,
        "wifi_ssid": wifi_ssid,
        "wifi_fallback_ssid": "",
        "device_token": secrets.token_urlsafe(32),
        "codex_poll_seconds": 300,
        "usb_pulse_seconds": 30,
        "battery_pulse_seconds": 60,
        "low_power_pulse_seconds": 300,
        "usb_brightness": 180,
        "battery_brightness": 130,
        "battery_idle_brightness": 75,
        "low_battery_brightness": 45,
        "battery_dim_after_seconds": 45,
        "screen_standby_after_seconds": 900,
        "ipad_featured_projects": [],
        "wake_word_enabled": False,
        "wake_on_battery": False,
        "low_battery_percent": 15,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Created private configuration: {path}")


if __name__ == "__main__":
    main()
