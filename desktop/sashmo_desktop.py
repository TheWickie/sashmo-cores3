"""Windows dashboard and launcher for the Sashmo desktop service."""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DESKTOP = ROOT / "desktop"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))
from sashmo.siwc import SIWCClient, WindowsTokenStore
from sashmo.gmail import GmailClient, GmailTokenStore
from sashmo.calendar import CalendarClient, CalendarTokenStore
from service_host import (autostart_enabled as backend_autostart_enabled,
                          send_command as send_service_command,
                          set_autostart as set_backend_autostart,
                          start_supervisor)
CONFIG = ROOT / "config.local.json"
KEY_FILE = ROOT / "openAIAPIKey.txt"
LOG_FILE = ROOT / "work" / "desktop-service.log"
PROJECTS_FILE = ROOT / "projects.local.json"
APP_NAME = "Sashmo"
_CONFIG_WRITE_LOCK = threading.RLock()

BG = "#0a1220"
PANEL = "#111e30"
PANEL_LIGHT = "#17283d"
TEXT = "#f2f6fa"
MUTED = "#a3b4c8"
ACCENT = "#55d6b4"
BLUE = "#77aef8"
AMBER = "#ffc86a"
RED = "#ff777d"
BORDER = "#2a3b50"


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def update_config(changes: dict) -> None:
    """Atomically merge dashboard settings without losing parallel OAuth edits."""
    with _CONFIG_WRITE_LOCK:
        config = load_config()
        config.update(changes)
        temp_dir = ROOT / "work"
        temp_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="sashmo-config-", suffix=".tmp", dir=temp_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(config, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, CONFIG)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _local_datetime(value: str | None) -> str:
    if not value:
        return "Not yet"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d %b %Y, %H:%M")
    except ValueError:
        return "Unavailable"


def _duration(minutes: int | None) -> str:
    if minutes == 10080:
        return "Weekly"
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440} day"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60} hour"
    return "Reported window"


def _main_windows(quota: dict | None) -> list[dict]:
    if not quota:
        return []
    bucket_id = quota.get("primary_bucket_id")
    for bucket in quota.get("buckets") or []:
        if bucket.get("id") == bucket_id:
            return bucket.get("windows") or []
    return []


class ServiceController:
    def __init__(self) -> None:
        self.lock = threading.Lock()

    def endpoint(self) -> tuple[str, dict[str, str]]:
        config = load_config()
        return (f"http://{config['bind_host']}:{int(config.get('port', 8765))}/v1/status",
                {"X-Sashmo-Token": config["device_token"]})

    def fetch(self) -> dict:
        url, headers = self.endpoint()
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=2.5) as response:
            return json.load(response)

    def fetch_projects(self) -> dict:
        url, headers = self.endpoint()
        request = urllib.request.Request(url.replace("/v1/status", "/v1/projects"), headers=headers)
        with urllib.request.urlopen(request, timeout=35) as response:
            return json.load(response)

    def port_occupied(self) -> bool:
        config = load_config()
        try:
            with socket.create_connection((config["bind_host"], int(config.get("port", 8765))), timeout=0.5):
                return True
        except OSError:
            return False

    def start(self) -> str:
        with self.lock:
            status = start_supervisor()
            if status.get("external"):
                return "Connected to an existing Sashmo service; independent host is waiting to take over"
            return "Independent desktop service running" if status.get("running") else "Starting independent desktop service"

    def stop(self) -> str:
        with self.lock:
            result = send_service_command("stop")
            return result.get("message", "Stopping desktop service")

    def restart(self) -> str:
        with self.lock:
            result = send_service_command("restart")
            return result.get("message", "Restarting desktop service")


def autostart_enabled() -> bool:
    return backend_autostart_enabled()


def set_autostart(enabled: bool) -> None:
    set_backend_autostart(enabled)


class Card(tk.Frame):
    def __init__(self, parent: tk.Widget, **grid_kwargs) -> None:
        super().__init__(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        if grid_kwargs:
            self.grid(**grid_kwargs)


class SashmoDesktop(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Sashmo")
        self.configure(bg=BG)
        self.geometry("1010x820")
        self.minsize(850, 720)
        self.controller = ServiceController()
        self.current_page = "overview"
        self.polling = False
        self.poll_after_id: str | None = None
        self.closing = False
        self.action_busy = False
        self.account_busy = False
        self.auto_service = True
        self.recovery_delay = 2.0
        self.next_recovery_at = 0.0
        self.results: queue.Queue[tuple] = queue.Queue()
        self.status: dict | None = None
        self.account_client = SIWCClient(WindowsTokenStore())
        self.google_secret_var = tk.StringVar(value=load_config().get("google_oauth_client_secret", ""))
        self.gmail_client = GmailClient(GmailTokenStore(), client_secret=self.google_secret_var.get())
        self.gmail_busy = False
        self.calendar_client = CalendarClient(CalendarTokenStore(), client_secret=self.google_secret_var.get())
        self.calendar_busy = False
        self.account_choices: dict[str, str] = {}
        self.available_models: dict[str, str] = {}
        self.last_message = "Starting Sashmo"

        self._build_shell()
        self._show_page("overview")
        self.after(100, self._start_service)
        self.after(900, self._begin_poll)
        self.after(100, self._drain_results)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _label(self, parent: tk.Widget, text: str, size: int = 11, color: str = TEXT,
               weight: str = "normal", **options) -> tk.Label:
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, weight), anchor="w", **options)

    def _button(self, parent: tk.Widget, text: str, command, primary: bool = False) -> tk.Button:
        return tk.Button(parent, text=text, command=command,
                         bg=ACCENT if primary else PANEL_LIGHT,
                         fg=BG if primary else TEXT, activebackground=BLUE,
                         activeforeground=BG, relief="flat", bd=0,
                         font=("Segoe UI", 10, "bold"), padx=16, pady=9, cursor="hand2")

    def _build_shell(self) -> None:
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=28, pady=(23, 18))
        self._label(header, "SASHMO", 22, TEXT, "bold").pack(side="left")
        self._label(header, "Voice assistant · CoreS3 + Windows", 10, MUTED).pack(side="left", padx=(16, 0), pady=(10, 0))
        self.connection_label = self._label(header, "Checking service…", 10, AMBER, "bold")
        self.connection_label.pack(side="right", pady=(10, 0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=(0, 22))
        sidebar = tk.Frame(body, bg=PANEL, width=184, highlightbackground=BORDER, highlightthickness=1)
        sidebar.pack(side="left", fill="y", padx=(0, 18))
        sidebar.pack_propagate(False)
        self._label(sidebar, "WORKSPACE", 9, MUTED, "bold").pack(anchor="w", padx=18, pady=(22, 9))
        self.nav_buttons: dict[str, tk.Button] = {}
        for name, title in (("overview", "Overview"), ("alerts", "Alerts"),
                            ("accounts", "ChatGPT"), ("gmail", "Gmail"),
                            ("calendar", "Calendar"), ("projects", "Projects"),
                            ("settings", "Settings"), ("diagnostics", "Diagnostics")):
            button = self._button(sidebar, title, lambda page=name: self._show_page(page))
            button.configure(anchor="w", padx=18)
            button.pack(fill="x", padx=9, pady=4)
            self.nav_buttons[name] = button
        tk.Frame(sidebar, bg=PANEL).pack(fill="both", expand=True)
        self._label(sidebar, "Sashmo desktop", 9, MUTED).pack(anchor="w", padx=18, pady=(0, 18))

        self.content = tk.Frame(body, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.pages: dict[str, tk.Frame] = {}
        self._build_overview()
        self._build_alerts()
        self._build_accounts()
        self._build_gmail()
        self._build_calendar()
        self._build_projects()
        self._build_settings()
        self._build_diagnostics()

    def _page(self, name: str) -> tk.Frame:
        page = tk.Frame(self.content, bg=BG)
        self.pages[name] = page
        return page

    def _heading(self, parent: tk.Widget, title: str, subtitle: str) -> None:
        self._label(parent, title, 19, TEXT, "bold").pack(anchor="w")
        self._label(parent, subtitle, 10, MUTED).pack(anchor="w", pady=(3, 16))

    def _build_overview(self) -> None:
        page = self._page("overview")
        self._heading(page, "Your assistant at a glance", "Live status from the Windows service and your CoreS3")
        action_row = tk.Frame(page, bg=BG)
        action_row.pack(fill="x", pady=(0, 17))
        self._button(action_row, "Start service", self._start_service, True).pack(side="left")
        self._button(action_row, "Stop service", self._stop_service).pack(side="left", padx=10)
        self._button(action_row, "Restart", self._restart_service).pack(side="left", padx=(0, 10))
        self._button(action_row, "Refresh", self._begin_poll).pack(side="left")
        self.service_message = self._label(action_row, "", 10, MUTED)
        self.service_message.pack(side="right")

        cards = tk.Frame(page, bg=BG)
        cards.pack(fill="both", expand=True)
        cards.grid_columnconfigure(0, weight=3)
        cards.grid_columnconfigure(1, weight=2)
        cards.grid_rowconfigure(0, minsize=280, weight=1)
        cards.grid_rowconfigure(1, minsize=120)
        cards.grid_rowconfigure(2, minsize=110)
        quota_card = Card(cards, row=0, column=0, sticky="nsew", padx=(0, 9), pady=(0, 12))
        device_card = Card(cards, row=0, column=1, sticky="nsew", padx=(9, 0), pady=(0, 12))
        self._label(quota_card, "CODEX ALLOWANCE", 10, MUTED, "bold").pack(anchor="w", padx=23, pady=(22, 10))
        self.quota_value = self._label(quota_card, "—", 42, TEXT, "bold")
        self.quota_value.pack(anchor="w", padx=23)
        self.quota_caption = self._label(quota_card, "Waiting for reported limits", 11, MUTED)
        self.quota_caption.pack(anchor="w", padx=23)
        self.quota_bar = tk.Canvas(quota_card, height=10, bg=PANEL_LIGHT, highlightthickness=0)
        self.quota_bar.pack(fill="x", padx=23, pady=(18, 16))
        self.quota_reset = self._label(quota_card, "Reset: —", 10, TEXT)
        self.quota_reset.pack(anchor="w", padx=23)
        self.quota_credits = self._label(quota_card, "Reset credits: —", 10, MUTED)
        self.quota_credits.pack(anchor="w", padx=23, pady=(5, 20))

        self._label(device_card, "CORES3", 10, MUTED, "bold").pack(anchor="w", padx=23, pady=(22, 10))
        self.device_state = self._label(device_card, "Waiting for device", 21, AMBER, "bold")
        self.device_state.pack(anchor="w", padx=23)
        self.device_seen = self._label(device_card, "Last seen: —", 10, MUTED)
        self.device_seen.pack(anchor="w", padx=23, pady=(5, 18))
        self.device_power = self._label(device_card, "Battery: —", 12, TEXT)
        self.device_power.pack(anchor="w", padx=23, pady=3)
        self.device_wifi = self._label(device_card, "Wi-Fi signal: —", 12, TEXT)
        self.device_wifi.pack(anchor="w", padx=23, pady=3)
        self.device_source = self._label(device_card, "Power source: —", 12, TEXT)
        self.device_source.pack(anchor="w", padx=23, pady=(3, 18))

        voice_card = Card(cards, row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self._label(voice_card, "VOICE", 10, MUTED, "bold").pack(anchor="w", padx=23, pady=(18, 8))
        self.voice_state = self._label(voice_card, "Tap the microphone on the CoreS3 to speak", 14, TEXT, "bold")
        self.voice_state.pack(anchor="w", padx=23)
        self.voice_note = self._label(voice_card, "OpenAI transcription · Cedar speech · Windows fallback", 10, MUTED)
        self.voice_note.pack(anchor="w", padx=23, pady=(5, 18))

        codex_card = Card(cards, row=2, column=0, columnspan=2, sticky="ew")
        self._label(codex_card, "CONNECTION", 10, MUTED, "bold").pack(anchor="w", padx=23, pady=(18, 8))
        self.codex_state = self._label(codex_card, "Checking Codex", 12, TEXT)
        self.codex_state.pack(anchor="w", padx=23)
        self.codex_updated = self._label(codex_card, "Last quota update: —", 10, MUTED)
        self.codex_updated.pack(anchor="w", padx=23, pady=(4, 4))
        self.codex_tasks = self._label(codex_card, "Recent Codex tasks: —", 10, MUTED)
        self.codex_tasks.pack(anchor="w", padx=23, pady=(0, 18))

    def _build_settings(self) -> None:
        page = self._page("settings")
        self._heading(page, "Settings", "Local controls for this PC. Credentials stay outside the device.")
        card = Card(page)
        card.pack(fill="x")
        self._label(card, "DESKTOP SERVICE", 10, MUTED, "bold").pack(anchor="w", padx=24, pady=(22, 9))
        config = load_config()
        self.address_label = self._label(card, f"Home network: {config['bind_host']}:{config.get('port', 8765)}", 11, TEXT)
        self.address_label.pack(anchor="w", padx=24, pady=4)
        row = tk.Frame(card, bg=PANEL)
        row.pack(fill="x", padx=24, pady=(15, 4))
        self._label(row, "Codex refresh interval (seconds)", 11, TEXT).pack(side="left")
        self.poll_var = tk.StringVar(value=str(config.get("codex_poll_seconds", 300)))
        tk.Entry(row, textvariable=self.poll_var, width=8, bg=PANEL_LIGHT, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Segoe UI", 11), justify="center").pack(side="right")
        self._label(card, "Allowed range: 30–3600. Saving restarts a service launched by this dashboard.", 9, MUTED).pack(anchor="w", padx=24)
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        tk.Checkbutton(card, text="Keep the Sashmo backend running after Windows sign-in", variable=self.autostart_var,
                       bg=PANEL, fg=TEXT, selectcolor=PANEL_LIGHT, activebackground=PANEL,
                       activeforeground=TEXT, font=("Segoe UI", 11), anchor="w").pack(anchor="w", padx=20, pady=(22, 12))
        self._button(card, "Save settings", self._save_settings, True).pack(anchor="w", padx=24, pady=(0, 23))

        note = Card(page)
        note.pack(fill="x", pady=(16, 0))
        self._label(note, "AUDIO AND SECURITY", 10, MUTED, "bold").pack(anchor="w", padx=24, pady=(20, 8))
        key_text = "OpenAI speech key: available on this PC" if KEY_FILE.is_file() else "OpenAI speech key: missing; Windows fallback only"
        self._label(note, key_text, 11, TEXT).pack(anchor="w", padx=24, pady=4)
        self._label(note, "Cedar speech and transcription may use your API billing.", 10, MUTED).pack(anchor="w", padx=24, pady=4)
        self._label(note, "Conversational inference is disabled until a separate provider is configured.", 10, MUTED).pack(anchor="w", padx=24, pady=(4, 20))

        self._label(page, "Device pulse intervals are embedded in firmware; changing them requires a rebuild and flash.",
                    10, MUTED).pack(anchor="w", pady=(18, 0))

    def _build_projects(self) -> None:
        page = self._page("projects")
        self._heading(page, "Projects", "Only folders you add here can be inspected by Sashmo")
        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(0, 14))
        self._button(row, "Add Git project", self._add_project, True).pack(side="left")
        self._button(row, "Refresh", self._refresh_projects).pack(side="left", padx=10)
        self.projects_note = self._label(page, "Loading approved projects…", 11, MUTED)
        self.projects_note.pack(anchor="w", pady=(0, 12))
        self.projects_view = tk.Text(page, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                                     font=("Segoe UI", 11), wrap="word", padx=18, pady=16)
        self.projects_view.pack(fill="both", expand=True)
        self.projects_view.configure(state="disabled")

    def _build_accounts(self) -> None:
        page = self._page("accounts")
        self._heading(page, "ChatGPT plan", "Optional sign-in for natural replies through an eligible ChatGPT account")
        card = Card(page)
        card.pack(fill="x")
        self._label(card, "ACCOUNT", 10, MUTED, "bold").pack(anchor="w", padx=24, pady=(22, 8))
        self.account_status = self._label(card, "No account connected", 12, TEXT)
        self.account_status.pack(anchor="w", padx=24, pady=(0, 10))
        self.account_var = tk.StringVar()
        self.account_box = ttk.Combobox(card, textvariable=self.account_var, state="readonly", width=42)
        self.account_box.pack(anchor="w", padx=24, pady=(0, 14))
        self.account_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_models())
        row = tk.Frame(card, bg=PANEL)
        row.pack(fill="x", padx=24, pady=(0, 20))
        self._button(row, "Continue with ChatGPT", self._sign_in_chatgpt, True).pack(side="left")
        self._button(row, "Forget on this PC", self._forget_account).pack(side="left", padx=10)

        models = Card(page)
        models.pack(fill="x", pady=(16, 0))
        self._label(models, "CONVERSATION MODEL", 10, MUTED, "bold").pack(anchor="w", padx=24, pady=(22, 8))
        self.model_status = self._label(models, "Sign in to see available models", 11, TEXT)
        self.model_status.pack(anchor="w", padx=24, pady=(0, 8))
        self.model_var = tk.StringVar()
        self.model_box = ttk.Combobox(models, textvariable=self.model_var, state="readonly", width=42)
        self.model_box.pack(anchor="w", padx=24, pady=(0, 12))
        self._button(models, "Use selected model", self._select_model, True).pack(anchor="w", padx=24, pady=(0, 22))
        self._label(page, "Basic quota, time and connection answers stay local. A selected model handles other "
                    "questions through your ChatGPT plan. It never falls back to billed text API inference.",
                    10, MUTED, wraplength=620, justify="left").pack(anchor="w", pady=(18, 0))

    def _build_alerts(self) -> None:
        page = self._page("alerts")
        self._heading(page, "Alerts", "Current read-only notifications from the local service")
        self.alerts_note = self._label(page, "Waiting for service…", 11, MUTED)
        self.alerts_note.pack(anchor="w", pady=(0, 12))
        self.alerts_view = tk.Text(page, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                                   font=("Segoe UI", 11), wrap="word", padx=18, pady=16)
        self.alerts_view.pack(fill="both", expand=True)
        self.alerts_view.configure(state="disabled")

    def _build_gmail(self) -> None:
        page = self._page("gmail")
        self._heading(page, "Gmail", "Optional, read-only message headers and unread count")
        card = Card(page)
        card.pack(fill="x")
        self._label(card, "GOOGLE DESKTOP OAUTH", 10, MUTED, "bold").pack(anchor="w", padx=24, pady=(20, 8))
        self._label(card, "Create a Google Cloud Desktop OAuth client with the Gmail API enabled, "
                    "then paste its client ID here.", 10, TEXT, wraplength=610,
                    justify="left").pack(anchor="w", padx=24)
        self.gmail_client_var = tk.StringVar(value=load_config().get("google_oauth_client_id", ""))
        tk.Entry(card, textvariable=self.gmail_client_var, width=65, bg=PANEL_LIGHT, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(12, 12))
        self._label(card, "Desktop client secret (if Google requires it)", 10, MUTED).pack(anchor="w", padx=24)
        tk.Entry(card, textvariable=self.google_secret_var, width=65, show="*", bg=PANEL_LIGHT, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(8, 12))
        row = tk.Frame(card, bg=PANEL)
        row.pack(fill="x", padx=24, pady=(0, 18))
        self._button(row, "Connect Gmail", self._sign_in_gmail, True).pack(side="left")
        self._button(row, "Forget on this PC", self._forget_gmail).pack(side="left", padx=10)
        self.gmail_status = self._label(page, "Gmail not connected", 11, MUTED)
        self.gmail_status.pack(anchor="w", pady=(16, 12))
        self.gmail_view = tk.Text(page, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                                  font=("Segoe UI", 10), wrap="word", padx=18, pady=14)
        self.gmail_view.pack(fill="both", expand=True)
        self.gmail_view.configure(state="disabled")
        self._label(page, "This first step requests Gmail metadata only. It cannot read message bodies, "
                    "search mail or send messages. Google may require verification for public use.",
                    10, MUTED, wraplength=620, justify="left").pack(anchor="w", pady=(12, 0))

    def _render_gmail(self) -> None:
        gmail = (self.status or {}).get("gmail") or {}
        snapshot = gmail.get("snapshot") or {}
        if gmail.get("connected"):
            self.gmail_status.configure(text=f"Connected · {snapshot.get('unread_count', 0)} unread · "
                                        "recent headers below", fg=ACCENT)
        else:
            note = gmail.get("error") or "Gmail not connected"
            if snapshot.get("messages"):
                note += " · cached headers below may be old"
            self.gmail_status.configure(text=note, fg=MUTED)
        lines = []
        for item in snapshot.get("messages") or []:
            lines.append(f"{item.get('from') or 'Unknown sender'}\n"
                         f"{item.get('subject') or '(no subject)'}\n"
                         f"{item.get('date') or 'Date unavailable'}"
                         f"{' · unread' if item.get('unread') else ''}")
        content = "\n\n".join(lines) if lines else "No recent message headers available."
        self.gmail_view.configure(state="normal")
        self.gmail_view.delete("1.0", "end")
        self.gmail_view.insert("1.0", content)
        self.gmail_view.configure(state="disabled")

    def _sign_in_gmail(self) -> None:
        if self.gmail_busy:
            return
        client_id = self.gmail_client_var.get().strip()
        secret = self.google_secret_var.get().strip()
        if not client_id.endswith(".apps.googleusercontent.com"):
            self.gmail_status.configure(text="Enter a Google Desktop OAuth client ID first", fg=AMBER)
            return
        self.gmail_busy = True
        self.gmail_status.configure(text="Waiting for Google browser sign-in…", fg=AMBER)
        def work() -> None:
            try:
                update_config({"google_oauth_client_id": client_id,
                               "google_oauth_client_secret": secret})
                self.gmail_client.client_secret = secret
                attempt = self.gmail_client.begin_authorization(client_id)
                if not webbrowser.open(attempt.url):
                    attempt.close()
                    raise RuntimeError("Could not open the system browser")
                self.gmail_client.finish_authorization(attempt)
                self.results.put(("gmail_action", "Gmail connected; waiting for the next background refresh"))
            except Exception as exc:
                self.results.put(("gmail_action", f"Gmail connection failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _forget_gmail(self) -> None:
        if self.gmail_busy:
            return
        self.gmail_busy = True
        def work() -> None:
            try:
                self.gmail_client.forget()
                self.results.put(("gmail_action", "Local Gmail account removed"))
            except Exception as exc:
                self.results.put(("gmail_action", f"Could not remove Gmail account: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _build_calendar(self) -> None:
        page = self._page("calendar")
        self._heading(page, "Calendar", "Optional, read-only upcoming events")
        card = Card(page)
        card.pack(fill="x")
        self._label(card, "GOOGLE DESKTOP OAUTH", 10, MUTED, "bold").pack(anchor="w", padx=24, pady=(20, 8))
        self._label(card, "Enable the Google Calendar API for a Desktop OAuth client. "
                    "You may use the same client ID as Gmail, but consent is separate.",
                    10, TEXT, wraplength=610, justify="left").pack(anchor="w", padx=24)
        self.calendar_client_var = tk.StringVar(value=load_config().get("google_oauth_client_id", ""))
        tk.Entry(card, textvariable=self.calendar_client_var, width=65, bg=PANEL_LIGHT, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(12, 12))
        self._label(card, "Desktop client secret (if Google requires it)", 10, MUTED).pack(anchor="w", padx=24)
        tk.Entry(card, textvariable=self.google_secret_var, width=65, show="*", bg=PANEL_LIGHT, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Segoe UI", 10)).pack(anchor="w", padx=24, pady=(8, 12))
        row = tk.Frame(card, bg=PANEL)
        row.pack(fill="x", padx=24, pady=(0, 18))
        self._button(row, "Connect Calendar", self._sign_in_calendar, True).pack(side="left")
        self._button(row, "Forget on this PC", self._forget_calendar).pack(side="left", padx=10)
        self.calendar_status = self._label(page, "Calendar not connected", 11, MUTED)
        self.calendar_status.pack(anchor="w", pady=(16, 12))
        self.calendar_view = tk.Text(page, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                                     font=("Segoe UI", 10), wrap="word", padx=18, pady=14)
        self.calendar_view.pack(fill="both", expand=True)
        self.calendar_view.configure(state="disabled")
        self._label(page, "The first connection shows up to 20 events from your primary calendar "
                    "over the next 48 hours. Event descriptions and attendees are not requested.",
                    10, MUTED, wraplength=620, justify="left").pack(anchor="w", pady=(12, 0))

    def _render_calendar(self) -> None:
        calendar = (self.status or {}).get("calendar") or {}
        snapshot = calendar.get("snapshot") or {}
        if calendar.get("connected"):
            self.calendar_status.configure(text=f"Connected · {len(snapshot.get('events') or [])} upcoming event(s)",
                                           fg=ACCENT)
        else:
            note = calendar.get("error") or "Calendar not connected"
            if snapshot.get("events"):
                note += " · cached events below may be old"
            self.calendar_status.configure(text=note, fg=MUTED)
        lines = []
        for item in snapshot.get("events") or []:
            start = item.get("start") or "Time unavailable"
            lines.append(f"{item.get('title') or 'Event'}\n{start}"
                         f"{' · all day' if item.get('all_day') else ''}")
        content = "\n\n".join(lines) if lines else "No upcoming events available."
        self.calendar_view.configure(state="normal")
        self.calendar_view.delete("1.0", "end")
        self.calendar_view.insert("1.0", content)
        self.calendar_view.configure(state="disabled")

    def _sign_in_calendar(self) -> None:
        if self.calendar_busy:
            return
        client_id = self.calendar_client_var.get().strip()
        secret = self.google_secret_var.get().strip()
        if not client_id.endswith(".apps.googleusercontent.com"):
            self.calendar_status.configure(text="Enter a Google Desktop OAuth client ID first", fg=AMBER)
            return
        self.calendar_busy = True
        self.calendar_status.configure(text="Waiting for Google browser sign-in…", fg=AMBER)
        def work() -> None:
            try:
                update_config({"google_oauth_client_id": client_id,
                               "google_oauth_client_secret": secret})
                self.calendar_client.client_secret = secret
                attempt = self.calendar_client.begin_authorization(client_id)
                if not webbrowser.open(attempt.url):
                    attempt.close()
                    raise RuntimeError("Could not open the system browser")
                self.calendar_client.finish_authorization(attempt)
                self.results.put(("calendar_action", "Calendar connected; waiting for the next background refresh"))
            except Exception as exc:
                self.results.put(("calendar_action", f"Calendar connection failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _forget_calendar(self) -> None:
        if self.calendar_busy:
            return
        self.calendar_busy = True
        def work() -> None:
            try:
                self.calendar_client.forget()
                self.results.put(("calendar_action", "Local Calendar account removed"))
            except Exception as exc:
                self.results.put(("calendar_action", f"Could not remove Calendar account: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _render_alerts(self) -> None:
        alerts = (self.status or {}).get("notifications") or {}
        items = alerts.get("items") or []
        self.alerts_note.configure(text=f"{len(items)} current alert(s)" if self.status else
                                   "Desktop service unavailable")
        content = "\n\n".join(f"{item.get('title') or 'Alert'}\n{item.get('summary') or ''}"
                               for item in items) if items else "Nothing needs attention."
        self.alerts_view.configure(state="normal")
        self.alerts_view.delete("1.0", "end")
        self.alerts_view.insert("1.0", content)
        self.alerts_view.configure(state="disabled")

    def _refresh_accounts(self) -> None:
        def work() -> None:
            try:
                self.results.put(("accounts", self.account_client.account_summaries(), None))
            except Exception as exc:
                self.results.put(("accounts", None, type(exc).__name__))
        threading.Thread(target=work, daemon=True).start()

    def _render_accounts(self, accounts: list[dict] | None, error: str | None) -> None:
        if error:
            self.account_status.configure(text="Saved account could not be read; check Diagnostics", fg=AMBER)
            return
        self.account_choices = {}
        selected = ""
        for account in accounts or []:
            label = f"{account.get('email') or 'ChatGPT account'} · {account['key'][:6]}"
            self.account_choices[label] = account["key"]
            if account.get("active"):
                selected = label
        self.account_box["values"] = list(self.account_choices)
        self.account_var.set(selected)
        if selected:
            active = next(account for account in accounts or [] if account["key"] == self.account_choices[selected])
            model = active.get("model")
            self.account_status.configure(text="Connected · model selected" if model else
                                          "Connected · choose a model to enable conversation", fg=ACCENT)
            self._refresh_models()
        else:
            self.account_status.configure(text="No ChatGPT account connected", fg=MUTED)
            self.model_status.configure(text="Sign in to see available models")
            self.model_box["values"] = []
            self.model_var.set("")

    def _refresh_models(self) -> None:
        key = self.account_choices.get(self.account_var.get())
        if not key:
            return
        self.model_status.configure(text="Checking models available to this account…")
        def work() -> None:
            try:
                self.results.put(("models", self.account_client.list_models(key), None))
            except Exception as exc:
                self.results.put(("models", None, type(exc).__name__))
        threading.Thread(target=work, daemon=True).start()

    def _render_models(self, models: list[dict] | None, error: str | None) -> None:
        if error:
            self.model_status.configure(text="Model list unavailable. Reconnect this account and retry.")
            return
        self.available_models = {f"{item['display_name']} · {item['slug']}": item["slug"]
                                 for item in models or []}
        self.model_box["values"] = list(self.available_models)
        self.model_status.configure(text=f"{len(self.available_models)} available model(s). Select one to enable conversation.")
        self.model_var.set("")

    def _sign_in_chatgpt(self) -> None:
        if self.account_busy:
            return
        self.account_busy = True
        self.account_status.configure(text="Waiting for browser sign-in…", fg=AMBER)
        def work() -> None:
            try:
                attempt = self.account_client.begin_authorization()
                if not webbrowser.open(attempt.url):
                    attempt.close()
                    raise RuntimeError("Could not open the system browser")
                self.account_client.finish_authorization(attempt)
                self.results.put(("account_action", "ChatGPT sign-in completed"))
            except Exception as exc:
                self.results.put(("account_action", f"ChatGPT sign-in failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _select_model(self) -> None:
        key = self.account_choices.get(self.account_var.get())
        model = self.available_models.get(self.model_var.get())
        if not key or not model:
            self.model_status.configure(text="Choose an account and a listed model first")
            return
        def work() -> None:
            try:
                self.account_client.select_model(key, model)
                self.results.put(("account_action", "Conversation model enabled"))
            except Exception as exc:
                self.results.put(("account_action", f"Could not select model: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _forget_account(self) -> None:
        key = self.account_choices.get(self.account_var.get())
        if not key:
            return
        def work() -> None:
            try:
                self.account_client.forget_account(key)
                self.results.put(("account_action", "Local ChatGPT account removed"))
            except Exception as exc:
                self.results.put(("account_action", f"Could not remove account: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _set_projects_text(self, value: str) -> None:
        self.projects_view.configure(state="normal")
        self.projects_view.delete("1.0", "end")
        self.projects_view.insert("1.0", value)
        self.projects_view.configure(state="disabled")

    def _refresh_projects(self) -> None:
        self.projects_note.configure(text="Reading approved project status…")

        def work() -> None:
            try:
                result = self.controller.fetch_projects()
                self.results.put(("projects", result, None))
            except Exception as exc:
                self.results.put(("projects", None, type(exc).__name__))

        threading.Thread(target=work, daemon=True).start()

    def _render_projects(self, result: dict | None, error: str | None) -> None:
        if error:
            self.projects_note.configure(text="Desktop service unavailable. Start it and refresh.")
            self._set_projects_text("Project status could not be loaded.")
            return
        registry_error = (result or {}).get("error")
        if registry_error == "not_configured":
            self.projects_note.configure(text="No projects approved yet")
            self._set_projects_text("Choose Add Git project to allow Sashmo to report a folder's Git status.\n"
                                    "Sashmo reads the branch, last commit time, and whether files changed."
                                    " It does not read source files or run project commands.")
            return
        if registry_error:
            self.projects_note.configure(text="The private project list needs attention")
            self._set_projects_text(f"Registry error: {registry_error}. Add or fix the local project list.")
            return
        projects = (result or {}).get("projects") or []
        self.projects_note.configure(text=f"{len(projects)} approved project(s)")
        lines = []
        for project in projects:
            name = project.get("name") or "Unnamed project"
            state = project.get("state")
            if state == "ok":
                change = "Uncommitted changes" if project.get("dirty") else "Working tree clean"
                lines.append(f"{name}\n  Branch: {project.get('branch') or 'unknown'} · {change}\n"
                             f"  Last commit: {project.get('last_commit') or 'none'}")
            else:
                lines.append(f"{name}\n  Status unavailable: {state}")
        self._set_projects_text("\n\n".join(lines) if lines else "No projects are listed yet.")

    def _add_project(self) -> None:
        selected = filedialog.askdirectory(parent=self, title="Choose a Git project folder", mustexist=True)
        if not selected:
            return
        path = Path(selected).resolve()
        try:
            result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=5, check=True)
            if Path(result.stdout.strip()).resolve() != path:
                raise ValueError("Choose the top folder of a Git project")
            data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8")) if PROJECTS_FILE.exists() else {
                "allowed_roots": [], "projects": []}
            if not isinstance(data.get("projects"), list) or not isinstance(data.get("allowed_roots"), list):
                raise ValueError("The private project list is invalid")
            if any(Path(item.get("path", "")).resolve() == path for item in data["projects"]):
                raise ValueError("This project is already approved")
            if len(data["projects"]) >= 32:
                raise ValueError("The project list is full")
            base = re.sub(r"[^a-z0-9_-]+", "-", path.name.lower()).strip("-")[:50] or "project"
            used = {item.get("id") for item in data["projects"]}
            project_id = base
            index = 2
            while project_id in used:
                project_id = f"{base}-{index}"
                index += 1
            data["allowed_roots"].append(str(path))
            data["projects"].append({"id": project_id, "name": path.name, "path": str(path)})
            PROJECTS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            self._refresh_projects()
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            messagebox.showerror("Sashmo projects", f"Could not add project: {exc}", parent=self)

    def _build_diagnostics(self) -> None:
        page = self._page("diagnostics")
        self._heading(page, "Diagnostics", "Connection information without passwords, tokens or transcripts")
        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(0, 14))
        self._button(row, "Refresh log", self._refresh_log).pack(side="left")
        self._button(row, "Open project folder", lambda: os.startfile(ROOT)).pack(side="left", padx=10)
        self.diagnostic_summary = self._label(page, "Checking service…", 11, TEXT)
        self.diagnostic_summary.pack(anchor="w", pady=(0, 12))
        self.log_view = tk.Text(page, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                                font=("Consolas", 9), wrap="word", padx=14, pady=12)
        self.log_view.pack(fill="both", expand=True)
        self.log_view.configure(state="disabled")
        self._refresh_log()

    def _show_page(self, page: str) -> None:
        for name, frame in self.pages.items():
            frame.pack_forget()
            button = self.nav_buttons[name]
            button.configure(bg=PANEL_LIGHT if name == page else PANEL, fg=ACCENT if name == page else TEXT)
        self.pages[page].pack(fill="both", expand=True)
        self.current_page = page
        if page == "diagnostics":
            self._refresh_log()
        elif page == "projects":
            self._refresh_projects()
        elif page == "accounts":
            self._refresh_accounts()
        elif page == "alerts":
            self._render_alerts()
        elif page == "gmail":
            self._render_gmail()
        elif page == "calendar":
            self._render_calendar()

    def _action(self, action) -> None:
        if self.action_busy or self.closing:
            return
        self.action_busy = True

        def work() -> None:
            try:
                message = action()
            except Exception as exc:
                message = f"Could not manage service: {exc}"
            self.results.put(("action", message))

        threading.Thread(target=work, daemon=True).start()

    def _start_service(self) -> None:
        self.auto_service = True
        self._action(self.controller.start)

    def _stop_service(self) -> None:
        self.auto_service = False
        self._action(self.controller.stop)

    def _restart_service(self) -> None:
        self.auto_service = True
        self._action(self.controller.restart)

    def _action_done(self, message: str) -> None:
        self.action_busy = False
        self.last_message = message
        self.service_message.configure(text=message)
        self._begin_poll()

    def _begin_poll(self) -> None:
        if self.poll_after_id:
            self.after_cancel(self.poll_after_id)
            self.poll_after_id = None
        if self.polling or self.closing:
            return
        self.polling = True

        def work() -> None:
            try:
                result = self.controller.fetch()
                error = None
            except Exception as exc:
                result = None
                error = type(exc).__name__
            self.results.put(("poll", result, error))

        threading.Thread(target=work, daemon=True).start()

    def _poll_done(self, result: dict | None, error: str | None) -> None:
        self.polling = False
        self.status = result
        self._render_status(error)
        self._render_alerts()
        self._render_gmail()
        self._render_calendar()
        if result:
            self.recovery_delay = 2.0
            self.next_recovery_at = 0.0
        elif self.auto_service and not self.action_busy:
            now = time.monotonic()
            if now >= self.next_recovery_at:
                self._action(self.controller.start)
                self.next_recovery_at = now + self.recovery_delay
                self.recovery_delay = min(self.recovery_delay * 2, 60.0)
        self.poll_after_id = self.after(2500, self._begin_poll)

    def _drain_results(self) -> None:
        if self.closing:
            return
        try:
            while True:
                item = self.results.get_nowait()
                if item[0] == "action":
                    self._action_done(item[1])
                elif item[0] == "poll":
                    self._poll_done(item[1], item[2])
                elif item[0] == "projects":
                    self._render_projects(item[1], item[2])
                elif item[0] == "accounts":
                    self._render_accounts(item[1], item[2])
                elif item[0] == "models":
                    self._render_models(item[1], item[2])
                elif item[0] == "account_action":
                    self.account_busy = False
                    self.account_status.configure(text=item[1], fg=ACCENT if "failed" not in item[1]
                                                  and "Could not" not in item[1] else AMBER)
                    self._refresh_accounts()
                elif item[0] == "gmail_action":
                    self.gmail_busy = False
                    self.gmail_status.configure(text=item[1], fg=ACCENT if "failed" not in item[1]
                                                and "Could not" not in item[1] else AMBER)
                elif item[0] == "calendar_action":
                    self.calendar_busy = False
                    self.calendar_status.configure(text=item[1], fg=ACCENT if "failed" not in item[1]
                                                   and "Could not" not in item[1] else AMBER)
        except queue.Empty:
            pass
        self.after(100, self._drain_results)

    def _render_status(self, error: str | None) -> None:
        if not self.status:
            self.connection_label.configure(text="Desktop service offline", fg=RED)
            self.quota_value.configure(text="—")
            self.quota_caption.configure(text="Start the service to see your allowance")
            self.quota_reset.configure(text="Reset: —")
            self.quota_credits.configure(text="Reset credits: —")
            self._draw_bar(0)
            self.device_state.configure(text="Unavailable", fg=AMBER)
            self.device_seen.configure(text="Last seen: —")
            self.device_power.configure(text="Battery: —")
            self.device_wifi.configure(text="Wi-Fi signal: —")
            self.device_source.configure(text="Power source: —")
            self.voice_state.configure(text="Desktop service unavailable")
            self.codex_state.configure(text="Service unavailable")
            self.codex_updated.configure(text="Last quota update: —")
            self.codex_tasks.configure(text="Recent Codex tasks: —")
            self.diagnostic_summary.configure(text=f"Service unavailable ({error or 'no response'}). {self.last_message}")
            return
        status = self.status
        self.connection_label.configure(text="Desktop service running", fg=ACCENT)
        if self.last_message == "Starting desktop service":
            self.last_message = "Service ready"
            self.service_message.configure(text=self.last_message)
        codex = status.get("codex") or {}
        quota = codex.get("quota") or {}
        windows = _main_windows(quota)
        if windows:
            window = windows[0]
            remaining = window.get("remaining_percent")
            self.quota_value.configure(text=f"{remaining}%" if remaining is not None else "—")
            label = _duration(window.get("duration_mins"))
            plan = quota.get("plan_reported")
            self.quota_caption.configure(text=f"{label} window · reported plan: {plan or 'unknown'}")
            self.quota_reset.configure(text=f"Resets: {_local_datetime(window.get('resets_at_local'))}")
            self._draw_bar(int(remaining or 0))
        else:
            self.quota_value.configure(text="—")
            self.quota_caption.configure(text="No Codex usage window reported")
            self.quota_reset.configure(text="Reset: not reported")
            self._draw_bar(0)
        credits = quota.get("reset_credits_available")
        self.quota_credits.configure(text=f"Reset credits available: {credits}" if credits is not None else "Reset credits: not reported")
        self.codex_state.configure(text="Codex connected" if codex.get("connected") else
                                   f"Codex unavailable · {codex.get('error') or 'retrying'}",
                                   fg=ACCENT if codex.get("connected") else AMBER)
        self.codex_updated.configure(text=f"Last quota update: {_local_datetime(codex.get('last_updated_local'))}")
        sessions = status.get("codex_sessions") or {}
        if sessions.get("error"):
            self.codex_tasks.configure(text="Recent Codex tasks: unavailable")
        else:
            items = sessions.get("items") or []
            active = sum(item.get("state") == "in_progress" for item in items)
            self.codex_tasks.configure(text=f"Recent Codex tasks: {active} active · {len(items)} listed")

        device = status.get("device") or {}
        recent = bool(device.get("connected_recently"))
        self.device_state.configure(text="Connected" if recent else "Not recently seen", fg=ACCENT if recent else AMBER)
        age = device.get("last_seen_seconds_ago")
        self.device_seen.configure(text=f"Last pulse: {age}s ago" if age is not None else "No device pulse yet")
        battery = device.get("battery_percent")
        suffix = " · charging" if device.get("charging") else ""
        self.device_power.configure(text=f"Battery: {battery}%{suffix}" if battery is not None else "Battery: not reported")
        rssi = device.get("wifi_rssi")
        self.device_wifi.configure(text=f"Wi-Fi signal: {rssi} dBm" if rssi is not None else "Wi-Fi signal: not reported")
        source = device.get("usb_powered")
        self.device_source.configure(text="Power source: USB" if source is True else
                                     "Power source: battery" if source is False else "Power source: not reported")

        voice = status.get("voice") or {}
        voice_text = "Processing a voice request" if voice.get("state") == "processing" else "Ready for push-to-talk"
        self.voice_state.configure(text=voice_text)
        self.voice_note.configure(text=("OpenAI transcription · Cedar speech · Windows fallback" if KEY_FILE.is_file()
                                        else "Windows speech fallback · OpenAI key not found"))
        self.diagnostic_summary.configure(
            text=f"Service started {_local_datetime((status.get('service') or {}).get('started_at_local'))}"
                 f"  ·  Last device pulse {_local_datetime(device.get('last_seen_local'))}"
        )

    def _draw_bar(self, percent: int) -> None:
        self.quota_bar.delete("all")
        width = max(10, self.quota_bar.winfo_width())
        self.quota_bar.create_rectangle(0, 0, width, 10, fill=PANEL_LIGHT, outline="")
        self.quota_bar.create_rectangle(0, 0, width * max(0, min(100, percent)) / 100, 10,
                                        fill=ACCENT if percent > 20 else AMBER, outline="")

    def _refresh_log(self) -> None:
        try:
            data = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
            text = "\n".join(data) if data else "No service log yet."
        except OSError:
            text = "No service log yet."
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.insert("1.0", text)
        self.log_view.configure(state="disabled")
        self.log_view.see("end")

    def _save_settings(self) -> None:
        try:
            interval = int(self.poll_var.get())
            if not 30 <= interval <= 3600:
                raise ValueError("Codex refresh must be between 30 and 3600 seconds")
            update_config({"codex_poll_seconds": interval})
            set_autostart(self.autostart_var.get())
            try:
                managed = bool(send_service_command("status").get("managed"))
            except RuntimeError:
                managed = False
            if managed:
                self._action(self.controller.restart)
                messagebox.showinfo("Sashmo settings", "Settings saved. Restarting the desktop service.")
            else:
                messagebox.showinfo("Sashmo settings", "Settings saved. Restart the externally launched service to apply the interval.")
        except Exception as exc:
            messagebox.showerror("Sashmo settings", f"Could not save settings: {exc}")

    def _close(self) -> None:
        self.closing = True
        if self.poll_after_id:
            self.after_cancel(self.poll_after_id)
        self.destroy()


if __name__ == "__main__":
    if not CONFIG.is_file():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Sashmo", f"Missing {CONFIG}. Run backend/setup_config.py first.")
        root.destroy()
        raise SystemExit(1)
    SashmoDesktop().mainloop()
