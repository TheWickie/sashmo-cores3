"""Opt-in, read-only Google Calendar events for the Windows desktop.

Requires a Google Cloud Desktop OAuth client and enabled Calendar API. Uses a
separate grant from Gmail, requests only calendar.events.readonly, and never
fetches event descriptions, locations, attendees, or conference details.

https://developers.google.com/identity/protocols/oauth2/native-app
https://developers.google.com/workspace/calendar/api/auth
https://developers.google.com/workspace/calendar/api/v3/reference/events/list
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
import ctypes.wintypes
import hashlib
import hmac
import json
import os
import queue
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CALLBACK_PATH = "/calendar/callback"


class CalendarError(RuntimeError):
    """Safe, user-facing Calendar failure without private event or token text."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect(raw: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise CalendarError("Protected Calendar credential storage requires Windows")
    source = ctypes.create_string_buffer(raw)
    input_blob = _Blob(len(raw), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output_blob = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    method = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    method.restype = ctypes.wintypes.BOOL
    method.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
                       ctypes.POINTER(_Blob)]
    if not method(ctypes.byref(input_blob), None, None, None, None, 0,
                  ctypes.byref(output_blob)):
        raise CalendarError("Windows could not protect Calendar credentials")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


class CalendarTokenStore:
    """A Calendar-only grant encrypted for the current Windows user."""

    def __init__(self, path: Path | None = None) -> None:
        appdata = os.environ.get("LOCALAPPDATA")
        if path is None and (os.name != "nt" or not appdata):
            raise CalendarError("Windows LocalAppData is required for Calendar sign-in")
        self.path = path or Path(appdata) / "Sashmo" / "calendar-credentials.bin"
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def locked(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with self._lock, lock_path.open("a+b") as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def load(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.path.is_file():
                return None
            try:
                data = json.loads(_protect(self.path.read_bytes(), decrypt=True))
                if not isinstance(data, dict):
                    raise ValueError("Invalid account data")
                return data
            except (OSError, ValueError, UnicodeError) as exc:
                raise CalendarError("Saved Calendar credentials could not be read") from exc

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            encrypted = _protect(json.dumps(data, separators=(",", ":")).encode())
            fd, name = tempfile.mkstemp(prefix="calendar-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encrypted)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def forget(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)


@dataclass
class AuthorizationAttempt:
    url: str
    redirect_uri: str
    client_id: str
    state: str
    verifier: str
    server: HTTPServer = field(repr=False)
    callback: queue.Queue[dict[str, str]] = field(repr=False)
    consumed: bool = field(default=False, repr=False)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _callback_server() -> tuple[HTTPServer, queue.Queue[dict[str, str]]]:
    received: queue.Queue[dict[str, str]] = queue.Queue(maxsize=1)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if parsed.path != CALLBACK_PATH or self.headers.get("Host") != expected_host:
                self.send_error(404)
                return
            params = parse_qs(parsed.query, keep_blank_values=True)
            if received.full() or any(len(values) != 1 for values in params.values()):
                self.send_error(400)
                return
            received.put({key: values[0] for key, values in params.items()})
            body = b"<html><body>Calendar sign-in received. Return to Sashmo.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass  # Callback URLs contain authorization codes.

    server = HTTPServer(("127.0.0.1", 0), Handler)
    return server, received


class CalendarClient:
    """Calendar OAuth and bounded upcoming-event cache; no background loop."""

    def __init__(self, store: CalendarTokenStore, http: httpx.Client | None = None,
                 *, min_poll_seconds: float = 120) -> None:
        self.store = store
        self.http = http or httpx.Client(timeout=15, follow_redirects=False)
        self.min_poll_seconds = max(30, min_poll_seconds)
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0
        self._lock = threading.RLock()

    def _store_locked(self):
        lock = getattr(self.store, "locked", None)
        return lock() if lock else contextlib.nullcontext()

    def configured(self) -> bool:
        return self.store.load() is not None

    def begin_authorization(self, client_id: str) -> AuthorizationAttempt:
        if not client_id or not client_id.endswith(".apps.googleusercontent.com"):
            raise CalendarError("Set a Google Desktop OAuth client ID first")
        server, callback = _callback_server()
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        params = {"client_id": client_id, "redirect_uri": redirect_uri,
                  "response_type": "code", "scope": SCOPE,
                  "access_type": "offline", "prompt": "consent", "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return AuthorizationAttempt(AUTHORIZE_URL + "?" + urlencode(params),
                                    redirect_uri, client_id, state, verifier,
                                    server, callback)

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.http.post(TOKEN_URL, data=form)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid token response")
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise CalendarError("Calendar sign-in or renewal failed; sign in again") from exc

    def finish_authorization(self, attempt: AuthorizationAttempt,
                             timeout: float = 300) -> None:
        if attempt.consumed:
            raise CalendarError("Calendar sign-in attempt was already used")
        attempt.consumed = True
        try:
            try:
                params = attempt.callback.get(timeout=timeout)
            except queue.Empty as exc:
                raise CalendarError("Calendar sign-in timed out") from exc
        finally:
            attempt.close()
        if not hmac.compare_digest(params.get("state", ""), attempt.state):
            raise CalendarError("Calendar sign-in state did not match")
        if params.get("error"):
            raise CalendarError("Calendar sign-in was declined or cancelled")
        if not params.get("code"):
            raise CalendarError("Google did not return an authorization code")
        tokens = self._token_request({"client_id": attempt.client_id,
                                      "code": params["code"],
                                      "code_verifier": attempt.verifier,
                                      "redirect_uri": attempt.redirect_uri,
                                      "grant_type": "authorization_code"})
        if SCOPE not in str(tokens.get("scope", "")).split():
            raise CalendarError("Calendar read permission was not granted")
        if not all(isinstance(tokens.get(key), str) and tokens[key]
                   for key in ("access_token", "refresh_token")):
            raise CalendarError("Google did not return a renewable Calendar session")
        with self._lock:
            with self._store_locked():
                self.store.save({"client_id": attempt.client_id,
                                 "access_token": tokens["access_token"],
                                 "refresh_token": tokens["refresh_token"],
                                 "scope": SCOPE,
                                 "expires_at": time.time() + int(tokens.get("expires_in", 3600))})
            self._cache = None

    def forget(self) -> None:
        with self._lock:
            with self._store_locked():
                self.store.forget()
            self._cache = None

    def _access_token(self) -> str:
        with self._lock:
            with self._store_locked():
                data = self.store.load()
                if not data or data.get("scope") != SCOPE:
                    raise CalendarError("Connect Calendar in the desktop app first")
                if float(data.get("expires_at", 0)) - time.time() < 120:
                    tokens = self._token_request({"client_id": data["client_id"],
                                                  "refresh_token": data["refresh_token"],
                                                  "grant_type": "refresh_token"})
                    if not isinstance(tokens.get("access_token"), str):
                        raise CalendarError("Calendar session could not be renewed; sign in again")
                    if tokens.get("scope") and SCOPE not in str(tokens["scope"]).split():
                        raise CalendarError("Calendar read permission expired; sign in again")
                    data["access_token"] = tokens["access_token"]
                    if tokens.get("refresh_token"):
                        data["refresh_token"] = tokens["refresh_token"]
                    data["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))
                    self.store.save(data)
                return data["access_token"]

    def upcoming(self, *, hours: int = 24, force: bool = False) -> dict[str, Any]:
        """Return at most 20 events from the primary calendar within 24/48h.

        Descriptions and participants are excluded at the API level; private
        event titles are replaced with a generic label. Cache stays in memory.
        """
        if hours not in (24, 48):
            raise ValueError("Calendar window must be 24 or 48 hours")
        with self._lock:
            if (not force and self._cache is not None
                    and self._cache.get("hours") == hours
                    and time.monotonic() - self._cache_at < self.min_poll_seconds):
                return dict(self._cache)
            now = datetime.now(timezone.utc)
            params = {"timeMin": now.isoformat().replace("+00:00", "Z"),
                      "timeMax": (now + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
                      "singleEvents": "true", "orderBy": "startTime",
                      "showDeleted": "false", "maxResults": "20",
                      "fields": "items(id,status,visibility,summary,start,end),nextPageToken"}
            try:
                response = self.http.get(EVENTS_URL,
                                         headers={"Authorization": f"Bearer {self._access_token()}"},
                                         params=params)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
                    raise ValueError("Invalid event response")
            except (httpx.HTTPError, ValueError) as exc:
                raise CalendarError("Calendar is unavailable; check the connection and try again") from exc
            events: list[dict[str, Any]] = []
            for item in payload.get("items", [])[:20]:
                if not isinstance(item, dict) or item.get("status") == "cancelled":
                    continue
                start = item.get("start", {})
                end = item.get("end", {})
                if not isinstance(start, dict) or not isinstance(end, dict):
                    continue
                start_value = start.get("dateTime") or start.get("date")
                end_value = end.get("dateTime") or end.get("date")
                if not isinstance(start_value, str) or not isinstance(end_value, str):
                    continue
                private = item.get("visibility") in ("private", "confidential")
                events.append({"title": "Private event" if private else str(item.get("summary") or "Untitled event")[:160],
                               "start": start_value, "end": end_value,
                               "all_day": "date" in start})
            snapshot = {"hours": hours, "events": events, "checked_at": time.time(),
                        "truncated": bool(payload.get("nextPageToken"))}
            self._cache, self._cache_at = snapshot, time.monotonic()
            return dict(snapshot)
