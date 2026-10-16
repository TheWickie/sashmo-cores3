"""Opt-in, read-only Gmail metadata for the Windows desktop.

The Gmail API must be enabled and a *Desktop app* OAuth client ID configured
before sign-in. This module never reads mail bodies and never uses the speech
API key or ChatGPT sign-in. ``gmail.metadata`` is a restricted Google scope;
public distribution may require Google's verification. It does not allow the
Gmail messages.list ``q`` search parameter. Body access would require a new
``gmail.readonly`` grant and separate product work.

Google references:
https://developers.google.com/identity/protocols/oauth2/native-app
https://developers.google.com/workspace/gmail/api/auth/scopes
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
"""

from __future__ import annotations

import asyncio
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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx


SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALLBACK_PATH = "/gmail/callback"
HEADERS = ("From", "Subject", "Date")


class GmailError(RuntimeError):
    """User-facing failure whose message never includes tokens or mail data."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect(raw: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise GmailError("Protected Gmail credential storage requires Windows")
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
        raise GmailError("Windows could not protect Gmail credentials")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


class GmailTokenStore:
    """One Gmail account, encrypted for the current Windows user."""

    def __init__(self, path: Path | None = None) -> None:
        appdata = os.environ.get("LOCALAPPDATA")
        if path is None and (os.name != "nt" or not appdata):
            raise GmailError("Windows LocalAppData is required for Gmail sign-in")
        self.path = path or Path(appdata) / "Sashmo" / "gmail-credentials.bin"
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def locked(self):
        """Serialize refresh rotation between desktop processes."""
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
                raise GmailError("Saved Gmail credentials could not be read") from exc

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            encrypted = _protect(json.dumps(data, separators=(",", ":")).encode())
            fd, name = tempfile.mkstemp(prefix="gmail-", dir=self.path.parent)
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
            body = b"<html><body>Gmail sign-in received. Return to Sashmo.</body></html>"
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


class GmailClient:
    """Metadata-only Gmail client; callers decide when to poll or show mail."""

    def __init__(self, store: GmailTokenStore, http: httpx.Client | None = None,
                 *, min_poll_seconds: float = 120, client_secret: str = "") -> None:
        self.store = store
        self.http = http or httpx.Client(timeout=15, follow_redirects=False)
        self.client_secret = client_secret
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
        """Return URL for a system browser; caller retains and completes attempt."""
        if not client_id or not client_id.endswith(".apps.googleusercontent.com"):
            raise GmailError("Set a Google Desktop OAuth client ID first")
        server, callback = _callback_server()
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        params = {
            "client_id": client_id, "redirect_uri": redirect_uri,
            "response_type": "code", "scope": SCOPE,
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
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
            raise GmailError("Gmail sign-in or renewal failed; sign in again") from exc

    def finish_authorization(self, attempt: AuthorizationAttempt,
                             timeout: float = 300) -> None:
        if attempt.consumed:
            raise GmailError("Gmail sign-in attempt was already used")
        attempt.consumed = True
        try:
            try:
                params = attempt.callback.get(timeout=timeout)
            except queue.Empty as exc:
                raise GmailError("Gmail sign-in timed out") from exc
        finally:
            attempt.close()
        if not hmac.compare_digest(params.get("state", ""), attempt.state):
            raise GmailError("Gmail sign-in state did not match")
        if params.get("error"):
            raise GmailError("Gmail sign-in was declined or cancelled")
        if not params.get("code"):
            raise GmailError("Google did not return an authorization code")
        form = {
            "client_id": attempt.client_id, "code": params["code"],
            "code_verifier": attempt.verifier, "redirect_uri": attempt.redirect_uri,
            "grant_type": "authorization_code",
        }
        if self.client_secret:
            form["client_secret"] = self.client_secret
        tokens = self._token_request(form)
        if SCOPE not in str(tokens.get("scope", "")).split():
            raise GmailError("Gmail metadata permission was not granted")
        if not all(isinstance(tokens.get(key), str) and tokens[key]
                   for key in ("access_token", "refresh_token")):
            raise GmailError("Google did not return a renewable Gmail session")
        with self._lock:
            with self._store_locked():
                account = {
                    "client_id": attempt.client_id,
                    "access_token": tokens["access_token"],
                    "refresh_token": tokens["refresh_token"],
                    "scope": SCOPE,
                    "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
                }
                if self.client_secret:
                    account["client_secret"] = self.client_secret
                self.store.save(account)
            self._cache = None

    def forget(self) -> None:
        """Remove local grant; users can also revoke it in Google Account settings."""
        with self._lock:
            with self._store_locked():
                self.store.forget()
            self._cache = None

    def _access_token(self) -> str:
        with self._lock:
            with self._store_locked():
                data = self.store.load()
                if not data or data.get("scope") != SCOPE:
                    raise GmailError("Connect Gmail in the desktop app first")
                if float(data.get("expires_at", 0)) - time.time() < 120:
                    form = {
                        "client_id": data["client_id"],
                        "refresh_token": data["refresh_token"],
                        "grant_type": "refresh_token",
                    }
                    if data.get("client_secret"):
                        form["client_secret"] = data["client_secret"]
                    tokens = self._token_request(form)
                    if not isinstance(tokens.get("access_token"), str):
                        raise GmailError("Gmail session could not be renewed; sign in again")
                    if tokens.get("scope") and SCOPE not in str(tokens["scope"]).split():
                        raise GmailError("Gmail metadata permission expired; sign in again")
                    data["access_token"] = tokens["access_token"]
                    if tokens.get("refresh_token"):
                        data["refresh_token"] = tokens["refresh_token"]
                    data["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))
                    self.store.save(data)
                return data["access_token"]

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._access_token()
        try:
            response = self.http.get(GMAIL_URL + path,
                                     headers={"Authorization": f"Bearer {token}"},
                                     params=params)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid Gmail response")
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise GmailError("Gmail is unavailable; check the connection and try again") from exc

    def poll(self, *, force: bool = False) -> dict[str, Any]:
        """Return bounded inbox metadata; no body, snippet or search query."""
        with self._lock:
            if (not force and self._cache is not None
                    and time.monotonic() - self._cache_at < self.min_poll_seconds):
                return dict(self._cache)
            label = self._get("/labels/INBOX")
            listing = self._get("/messages", params={"labelIds": "INBOX", "maxResults": 10})
            messages: list[dict[str, Any]] = []
            for item in listing.get("messages", [])[:10]:
                message_id = str(item.get("id", ""))
                if not message_id or not all(c.isalnum() or c in "-_" for c in message_id):
                    continue
                details = self._get("/messages/" + message_id,
                                    params={"format": "METADATA", "metadataHeaders": HEADERS})
                headers = {str(header.get("name", "")).lower(): str(header.get("value", ""))
                           for header in details.get("payload", {}).get("headers", [])
                           if str(header.get("name", "")).lower() in ("from", "subject", "date")}
                messages.append({
                    "id": message_id,
                    "from": headers.get("from", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                    "unread": "UNREAD" in details.get("labelIds", []),
                })
            snapshot = {"unread_count": max(0, int(label.get("messagesUnread", 0))),
                        "messages": messages, "checked_at": time.time()}
            self._cache, self._cache_at = snapshot, time.monotonic()
            return dict(snapshot)


class GmailWorker:
    """Poll independently of CoreS3 pulses; keep personal metadata on the PC."""

    def __init__(self, state: Any, poll_seconds: int = 300,
                 client: GmailClient | None = None) -> None:
        self.state = state
        self.poll_seconds = max(120, poll_seconds)
        self.client = client
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        next_poll_at = 0.0
        while True:
            try:
                if self.client is None:
                    self.client = GmailClient(GmailTokenStore())
                configured = await asyncio.to_thread(self.client.configured)
                if configured:
                    if time.monotonic() >= next_poll_at:
                        snapshot = await asyncio.to_thread(self.client.poll)
                        self.state.update_gmail(snapshot, None)
                        next_poll_at = time.monotonic() + self.poll_seconds
                else:
                    self.state.update_gmail(None, "Not connected")
                    self.client._cache = None
                    next_poll_at = 0.0
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except (GmailError, OSError, ValueError):
                self.state.update_gmail(None, "Gmail unavailable; reconnect in the dashboard")
                next_poll_at = time.monotonic() + self.poll_seconds
                await asyncio.sleep(15)
