"""One-time browser sign-in for the read-only Sashmo reminder check."""

from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from sashmo.chatgpt_reminders import browser_profile, validate_chat_url


def main() -> None:
    config = json.loads((ROOT / "config.local.json").read_text(encoding="utf-8-sig"))
    chat_url = validate_chat_url(str(config.get("chatgpt_reminder_chat_url") or ""))
    profile = browser_profile()
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False, args=["--no-first-run"]
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)
            root = tk.Tk()
            root.withdraw()
            while True:
                messagebox.showinfo(
                    "Connect Sashmo reminders",
                    "Sign in to ChatGPT in the browser window. Open the Sashmo Reminder Query chat. "
                    "Then click OK here to check the connection. Sashmo will keep this browser "
                    "profile on this PC for background checks.", parent=root,
                )
                if page.locator('[contenteditable="true"][role="textbox"]').count() and \
                        page.url.startswith(chat_url):
                    messagebox.showinfo("Sashmo reminders", "ChatGPT is signed in. Restart the "
                                        "Sashmo service to begin 15-minute checks.", parent=root)
                    break
                if not messagebox.askretrycancel("Sashmo reminders", "The dedicated chat is not open "
                                                 "and signed in yet. Try again?", parent=root):
                    break
            root.destroy()
        finally:
            context.close()


if __name__ == "__main__":
    main()
