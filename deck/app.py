"""Desktop preview, private settings and optional USB Stream Deck driver."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
import urllib.error
import urllib.request

from PIL import ImageTk

from .hardware import Hardware
from .model import Card, cards, load_settings, valid_chat_url
from .render import jpeg_key, preview


def _request(root: Path, path: str, *, post: bool = False, payload: dict | None = None) -> dict:
    config = json.loads((root / "config.local.json").read_text(encoding="utf-8"))
    url = f"http://{config['bind_host']}:{int(config.get('port', 8765))}{path}"
    headers = {"X-Sashmo-Token": config["device_token"]}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else (b"" if post else None),
                                     headers=headers,
                                     method="POST" if post else "GET")
    with urllib.request.urlopen(request, timeout=3) as reply:
        return json.load(reply)


def _elgato_running() -> bool:
    if os.name != "nt":
        return False
    command = ["tasklist", "/FI", "IMAGENAME eq StreamDeck.exe", "/FO", "CSV", "/NH"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=3,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "StreamDeck.exe" in done.stdout
    except (OSError, subprocess.TimeoutExpired):
        return True  # Fail closed: do not run competing USB key actions.


class DeckApp(tk.Tk):
    def __init__(self, root: Path, *, hardware: bool = False) -> None:
        super().__init__()
        self.project_root = root
        self.settings = load_settings(root)
        self.hardware_enabled = hardware
        self.deck: Hardware | None = None
        self.events: queue.Queue[tuple] = queue.Queue()
        self.commands: queue.Queue[tuple] = queue.Queue()
        self.stop_event = threading.Event()
        self.status: dict = {}
        self.cards: list[Card] = cards({}, self.settings)
        self.images: list[ImageTk.PhotoImage] = []
        self.last_jpegs: list[bytes | None] = [None] * 15
        self.title("Sashmo Deck")
        self.configure(bg="#08111f")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self._render()
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(100, self._drain)

    def _build(self) -> None:
        top = tk.Frame(self, bg="#08111f")
        top.pack(fill="x", padx=18, pady=(12, 4))
        tk.Label(top, text="SASHMO DECK", font=("Segoe UI", 17, "bold"),
                 fg="#eaf6ff", bg="#08111f").pack(side="left")
        self.health = tk.Label(top, text="Connecting…", font=("Segoe UI", 10),
                               fg="#7fa8bb", bg="#08111f")
        self.health.pack(side="right")
        self.preview_label = tk.Label(self, bg="#08111f")
        self.preview_label.pack(padx=8, pady=5)
        grid = tk.Frame(self, bg="#08111f")
        grid.pack(padx=20, pady=(2, 12))
        for i in range(15):
            button = tk.Button(grid, text=str(i+1), width=10, bg="#162a42", fg="#eaf6ff",
                               activebackground="#2c627d", relief="flat", cursor="hand2",
                               command=lambda n=i: self.commands.put(("press", n)))
            button.grid(row=i//5, column=i%5, padx=2, pady=2)
        actions = tk.Frame(self, bg="#08111f")
        actions.pack(fill="x", padx=20, pady=(0, 15))
        tk.Button(actions, text="Settings", command=self._settings, bg="#1b3548", fg="white",
                  relief="flat", padx=12, pady=6).pack(side="left")
        tk.Button(actions, text="Refresh", command=lambda: self.commands.put(("refresh",)),
                  bg="#1b3548", fg="white", relief="flat", padx=12, pady=6).pack(side="left", padx=6)
        self.usb_label = tk.Label(actions, text="Preview only" if not self.hardware_enabled else "USB starting…",
                                  fg="#7fa8bb", bg="#08111f")
        self.usb_label.pack(side="right")

    def _render(self) -> None:
        picture = preview(self.cards, self.settings["privacy"])
        photo = ImageTk.PhotoImage(picture)
        self.images = [photo]
        self.preview_label.configure(image=photo)

    def _drain(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "state":
                    self.status = event[1]
                    self.cards = cards(self.status, self.settings)
                    self._render()
                    self.health.configure(text="Service connected", fg="#2ce4a0")
                elif event[0] == "error":
                    self.health.configure(text=event[1], fg="#ffaf6d")
                elif event[0] == "usb":
                    self.usb_label.configure(text=event[1])
                elif event[0] == "show":
                    self._details(event[1])
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.after(100, self._drain)

    def _worker(self) -> None:
        next_fetch = 0.0
        next_probe = 0.0
        last_pressed = [False]*15
        drawn_cards: list[Card] = []
        drawn_privacy = None
        while not self.stop_event.is_set():
            try:
                if self.hardware_enabled and time.monotonic() >= next_probe:
                    next_probe = time.monotonic() + 5
                    if _elgato_running():
                        if self.deck:
                            self.deck.close()
                            self.deck = None
                        self.events.put(("usb", "Elgato app owns keys; preview only"))
                    elif not self.deck:
                        try:
                            self.deck = Hardware()
                            self.deck.open()
                            self.deck.set_brightness(self.settings["brightness"])
                            self.last_jpegs = [None]*15
                            drawn_cards = []
                            last_pressed = [False]*15
                            self.events.put(("usb", "15-key USB connected"))
                        except OSError:
                            self.deck = None
                            self.events.put(("usb", "USB unavailable; preview active"))
                if time.monotonic() >= next_fetch:
                    try:
                        snapshot = _request(self.project_root, "/v1/deck/status")
                        self.status = snapshot
                        self.cards = cards(snapshot, self.settings)
                        self.events.put(("state", snapshot))
                    except (OSError, ValueError, KeyError, urllib.error.HTTPError):
                        self.events.put(("error", "Service unavailable"))
                    next_fetch = time.monotonic() + 5
                if self.deck:
                    if self.cards != drawn_cards or self.settings["privacy"] != drawn_privacy:
                        for i, card in enumerate(self.cards):
                            encoded = jpeg_key(card, self.settings["privacy"])
                            if encoded != self.last_jpegs[i]:
                                self.deck.set_key(i, encoded)
                                self.last_jpegs[i] = encoded
                        drawn_cards = list(self.cards)
                        drawn_privacy = self.settings["privacy"]
                    current = self.deck.read_keys()
                    if current:
                        for i, pressed in enumerate(current):
                            if pressed and not last_pressed[i]:
                                self._action(i)
                        last_pressed = current
                try:
                    event = self.commands.get_nowait()
                except queue.Empty:
                    event = None
                if event:
                    if event[0] == "press":
                        self._action(event[1])
                    elif event[0] == "refresh":
                        next_fetch = 0
                    elif event[0] == "brightness" and self.deck:
                        self.deck.set_brightness(event[1])
                    elif event[0] == "redraw":
                        drawn_cards = []
                    elif event[0] == "mark_read":
                        try:
                            _request(self.project_root,
                                     f"/v1/deck/notifications/{event[1]}/read",
                                     post=True, payload={"read": True})
                            next_fetch = 0
                        except (OSError, ValueError, urllib.error.HTTPError):
                            self.events.put(("error", "Could not mark alert as read"))
                time.sleep(.05)
            except (OSError, ValueError):
                if self.deck:
                    self.deck.close()
                    self.deck = None
                    self.events.put(("usb", "USB disconnected; preview active"))
                next_probe = time.monotonic() + 2
                time.sleep(.5)
        if self.deck:
            self.deck.close()

    def _action(self, index: int) -> None:
        card = self.cards[index]
        if card.action == "talk":
            try:
                _request(self.project_root, "/v1/deck/talk", post=True)
                self.events.put(("usb", "Talk requested on CoreS3"))
            except (OSError, ValueError, urllib.error.HTTPError):
                self.events.put(("usb", "CoreS3 offline or busy"))
        elif card.action == "codex" and card.target:
            try:
                os.startfile("codex://threads/" + card.target)
            except OSError:
                self.events.put(("error", "Could not open Codex task"))
        elif card.action in {"alerts", "status"}:
            self.events.put(("show", card.action))
        elif card.action == "folder" and card.target and Path(card.target).is_dir():
            try:
                os.startfile(card.target)
            except OSError:
                self.events.put(("error", "Could not open project folder"))
        elif card.action == "url" and card.target:
            try:
                os.startfile(card.target)
            except OSError:
                self.events.put(("error", "Could not open link"))

    def _details(self, kind: str) -> None:
        window = tk.Toplevel(self)
        window.title("Sashmo alerts" if kind == "alerts" else "Sashmo status")
        window.configure(bg="#0c1a2c")
        window.geometry("620x420")
        if kind == "alerts":
            items = self.status.get("deck_notifications") or []
            tk.Label(window, text="ALERTS & UPDATES", fg="#eaf6ff", bg="#0c1a2c",
                     font=("Segoe UI", 16, "bold")).pack(anchor="w", padx=20, pady=(18, 8))
            listing = tk.Listbox(window, bg="#13293d", fg="#eaf6ff", selectbackground="#286378",
                                 font=("Segoe UI", 10), relief="flat")
            listing.pack(fill="both", expand=True, padx=20, pady=7)
            for item in items:
                marker = "●" if not item.get("read") else " "
                listing.insert("end", f"{marker} {item.get('priority','normal').upper()}  {item.get('title','')[:75]}")
            def mark_read() -> None:
                selected = listing.curselection()
                if not selected:
                    return
                identifier = items[selected[0]].get("id", "")
                if not identifier:
                    return
                self.commands.put(("mark_read", identifier))
                listing.delete(selected[0])
                listing.insert(selected[0], "  " + items[selected[0]].get("title", "")[:75])
            tk.Button(window, text="Mark selected read", command=mark_read, bg="#2adbc2",
                      fg="#061320", relief="flat", padx=12, pady=6).pack(anchor="e", padx=20, pady=(0, 15))
        else:
            codex = self.status.get("codex") or {}
            quota = codex.get("quota") or {}
            lines = ["Codex: " + ("connected" if codex.get("connected") else "unavailable")]
            for bucket in quota.get("buckets") or []:
                if bucket.get("id") != "codex":
                    continue
                for item in bucket.get("windows") or []:
                    minutes = item.get("duration_mins")
                    label = "Weekly" if minutes == 10080 else (f"{minutes} minutes" if minutes else "Reported window")
                    lines.append(f"{label}: {item.get('remaining_percent', '?')}% remaining; reset {item.get('resets_at_local') or 'not reported'}")
            sessions = (self.status.get("codex_sessions") or {}).get("items") or []
            lines.append("Codex tasks: " + ", ".join(str(item.get("state", "unknown")) for item in sessions[:5]))
            device = self.status.get("device") or {}
            lines.append("CoreS3: " + ("connected" if device.get("connected_recently") else "offline"))
            lines.append("Battery: " + (str(device["battery_percent"]) + "%" if device.get("battery_percent") is not None else "unknown"))
            lines.append("Gmail: " + ("connected" if (self.status.get("gmail") or {}).get("connected") else "unavailable"))
            lines.append("Calendar: " + ("connected" if (self.status.get("calendar") or {}).get("connected") else "unavailable"))
            text = tk.Text(window, bg="#13293d", fg="#eaf6ff", font=("Segoe UI", 11),
                           wrap="word", relief="flat", padx=15, pady=15)
            text.pack(fill="both", expand=True, padx=20, pady=20)
            text.insert("1.0", "\n\n".join(lines))
            text.configure(state="disabled")

    def _settings(self) -> None:
        window = tk.Toplevel(self)
        window.title("Stream Deck settings")
        window.configure(bg="#0c1a2c")
        window.resizable(False, False)
        rows = []
        def field(label: str, value: str) -> tk.Entry:
            row = tk.Frame(window, bg="#0c1a2c")
            row.pack(fill="x", padx=18, pady=3)
            tk.Label(row, text=label, bg="#0c1a2c", fg="white", width=19, anchor="w").pack(side="left")
            entry = tk.Entry(row, width=52)
            entry.insert(0, value)
            entry.pack(side="left")
            return entry
        for i in range(2):
            item = self.settings["projects"][i] if i < len(self.settings["projects"]) else {}
            rows.append((field(f"Project {i+1} name", item.get("name", "")),
                         field(f"Project {i+1} folder", item.get("path", ""))))
        chats = []
        for i in range(3):
            item = self.settings["chats"][i] if i < len(self.settings["chats"]) else {}
            chats.append((field(f"Chat {i+1} title", item.get("title", "")),
                          field(f"Chat {i+1} link", item.get("url", ""))))
        brightness = field("Brightness 0-100", str(self.settings["brightness"]))
        privacy = tk.BooleanVar(value=self.settings["privacy"])
        tk.Checkbutton(window, text="Hide personal labels on keys", variable=privacy,
                       bg="#0c1a2c", fg="white", selectcolor="#164353").pack(anchor="w", padx=18, pady=8)
        def save() -> None:
            try:
                projects = [{"name": n.get().strip(), "path": p.get().strip()}
                            for n, p in rows if n.get().strip() and p.get().strip()]
                saved_chats = [{"title": n.get().strip(), "url": u.get().strip()}
                               for n, u in chats if n.get().strip() and u.get().strip()]
                if any(not Path(p["path"]).is_dir() for p in projects):
                    raise ValueError("Choose an existing folder for each project")
                if any(not valid_chat_url(c["url"]) for c in saved_chats):
                    raise ValueError("Chat links must be saved chatgpt.com/c/... URLs")
                value = int(brightness.get())
                if not 0 <= value <= 100:
                    raise ValueError("Brightness must be 0-100")
                data = {"projects": projects, "chats": saved_chats,
                        "brightness": value, "privacy": privacy.get()}
                path = self.project_root / "deck.local.json"
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                self.settings = load_settings(self.project_root)
                self.cards = cards(self.status, self.settings)
                self._render()
                self.last_jpegs = [None]*15
                self.commands.put(("redraw",))
                self.commands.put(("brightness", value))
                window.destroy()
            except (OSError, ValueError) as exc:
                messagebox.showerror("Deck settings", str(exc), parent=window)
        tk.Button(window, text="Save", command=save, bg="#32d6bc", fg="#061320",
                  relief="flat", padx=22, pady=7).pack(pady=14)

    def _close(self) -> None:
        self.stop_event.set()
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sashmo Stream Deck preview and driver")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--hardware", action="store_true", help="Use USB only if Elgato app is closed")
    args = parser.parse_args()
    DeckApp(args.root, hardware=args.hardware).mainloop()


if __name__ == "__main__":
    main()
