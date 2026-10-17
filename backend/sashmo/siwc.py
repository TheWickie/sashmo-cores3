"""Opt-in Sign in with ChatGPT plan-usage client for the Windows desktop.

This module is deliberately not wired into Sashmo's voice path. A future UI must
obtain explicit account consent and select a model before calling ``stream_text``.
It never reads the Codex session or the speech API key.

Official flow: https://developers.openai.com/siwc/token-sharing-open-source
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
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt


ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"
AGENT_NAME = "Sashmo"


class SIWCError(RuntimeError):
    """An actionable sign-in or ChatGPT plan-usage failure (without secrets)."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Protect credentials for the current Windows user, with no plaintext file."""
    if os.name != "nt":
        raise SIWCError("Protected ChatGPT credential storage requires Windows")
    source = ctypes.create_string_buffer(data)
    input_blob = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output_blob = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    method = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    method.restype = ctypes.wintypes.BOOL
    method.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
                       ctypes.POINTER(_Blob)]
    ok = method(ctypes.byref(input_blob), None, None, None, None, 0,
                ctypes.byref(output_blob))
    if not ok:
        raise SIWCError("Windows could not protect ChatGPT credentials")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


class WindowsTokenStore:
    """DPAPI-encrypted account registry, atomically replaced under a process lock."""

    def __init__(self, path: Path | None = None) -> None:
        appdata_value = os.environ.get("LOCALAPPDATA")
        if path is None and (os.name != "nt" or not appdata_value):
            raise SIWCError("Windows LocalAppData is required for ChatGPT sign-in")
        self.path = path or Path(appdata_value) / "Sashmo" / "siwc-credentials.bin"
        self._mutex = threading.RLock()

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize refresh-token rotation between desktop processes."""
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with self._mutex, lock_path.open("a+b") as handle:
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

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"host_id": f"urn:uuid:{uuid.uuid4()}", "accounts": {}, "active": None}
        try:
            return json.loads(_dpapi(self.path.read_bytes(), decrypt=True))
        except (ValueError, OSError) as exc:
            raise SIWCError("Saved ChatGPT credentials cannot be read") from exc

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = _dpapi(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        fd, temp_path = tempfile.mkstemp(prefix="siwc-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


@dataclass
class AuthorizationAttempt:
    url: str
    redirect_uri: str
    client_id: str
    state: str
    nonce: str
    verifier: str
    selected_key: str | None
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
            body = b"<html><body>Sign-in received. Return to Sashmo.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass  # Callback URLs include authorization codes and must never be logged.

    server = HTTPServer(("127.0.0.1", 0), Handler)
    return server, received


class SIWCClient:
    def __init__(self, store: WindowsTokenStore, http: httpx.Client | None = None) -> None:
        self.store = store
        self.http = http or httpx.Client(timeout=15.0, follow_redirects=False)

    def account_summaries(self) -> list[dict[str, Any]]:
        """Public UI metadata only; never return tokens or the stable host ID."""
        with self.store.locked():
            data = self.store.load()
            return [{"key": key, "email": account.get("email"),
                     "active": data.get("active") == key,
                     "model": account.get("selected_model"),
                     "expires_at": account.get("expires_at")}
                    for key, account in data["accounts"].items()]

    def active_selection(self) -> tuple[str, str] | None:
        with self.store.locked():
            data = self.store.load()
            key = data.get("active")
            account = data["accounts"].get(key) if key else None
            model = account.get("selected_model") if account else None
            return (key, model) if isinstance(model, str) and model else None

    def select_account(self, key: str) -> None:
        with self.store.locked():
            data = self.store.load()
            if key not in data["accounts"]:
                raise SIWCError("That ChatGPT account is not saved")
            data["active"] = key
            self.store.save(data)

    def select_model(self, key: str, model: str) -> None:
        if model not in {item["slug"] for item in self.list_models(key)}:
            raise SIWCError("Select a model available to this ChatGPT account")
        with self.store.locked():
            data = self.store.load()
            if key not in data["accounts"]:
                raise SIWCError("That ChatGPT account is not saved")
            data["accounts"][key]["selected_model"] = model
            data["active"] = key
            self.store.save(data)

    def forget_account(self, key: str) -> None:
        """Remove local credentials; remote grant can be revoked in ChatGPT settings."""
        with self.store.locked():
            data = self.store.load()
            if key not in data["accounts"]:
                return
            del data["accounts"][key]
            if data.get("active") == key:
                data["active"] = next(iter(data["accounts"]), None)
            self.store.save(data)

    def begin_authorization(self, selected_key: str | None = None) -> AuthorizationAttempt:
        """Prepare loopback authorization; caller must open ``attempt.url`` in a browser."""
        with self.store.locked():
            data = self.store.load()
            self.store.save(data)  # Persist host ID before the first sign-in.
            saved = data["accounts"].get(selected_key) if selected_key else None
            if selected_key and not saved:
                raise SIWCError("That ChatGPT account is not saved")
        server, received = _callback_server()
        verifier, challenge = _pkce()
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        client_id = saved["client_id"] if saved else "dynamic_agent_client"
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
        params = {
            "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce,
            "code_challenge_method": "S256", "code_challenge": challenge,
            "ext_agent_host_id": data["host_id"],
        }
        if saved:
            if saved.get("id_token"):
                params["id_token_hint"] = saved["id_token"]
            if saved.get("email"):
                params["login_hint"] = saved["email"]
        else:
            params["agent_name_hint"] = AGENT_NAME
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return AuthorizationAttempt(AUTHORIZE_URL + "?" + urlencode(params), redirect_uri,
                                    client_id, state, nonce, verifier, selected_key,
                                    server, received)

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.http.post(TOKEN_URL, data=form)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Invalid token response")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise SIWCError("ChatGPT sign-in or token renewal failed; sign in again") from exc

    def _verify_id_token(self, token: str, client_id: str, nonce: str) -> dict[str, Any]:
        try:
            discovery = self.http.get(DISCOVERY_URL)
            discovery.raise_for_status()
            metadata = discovery.json()
            if metadata.get("issuer") != ISSUER or metadata.get("jwks_uri") != ISSUER + "/.well-known/jwks.json":
                raise SIWCError("Unexpected OpenAI identity configuration")
            jwks_response = self.http.get(metadata["jwks_uri"])
            jwks_response.raise_for_status()
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not header.get("kid"):
                raise SIWCError("Unsupported OpenAI ID token signature")
            keys = [key for key in jwks_response.json()["keys"]
                    if key.get("kid") == header["kid"] and key.get("kty") == "RSA"]
            if len(keys) != 1:
                raise SIWCError("OpenAI identity signing key was not found")
            claims = jwt.decode(token, jwt.PyJWK.from_dict(keys[0]).key,
                                algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                                leeway=5, options={"require": ["iss", "aud", "sub", "exp", "iat"]})
            if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
                raise SIWCError("ChatGPT sign-in nonce did not match")
            if not isinstance(claims.get("sub"), str) or not claims["sub"]:
                raise SIWCError("ChatGPT identity was missing")
            return claims
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise SIWCError("ChatGPT identity could not be verified") from exc

    def finish_authorization(self, attempt: AuthorizationAttempt, timeout: float = 300) -> str:
        """Consume the callback once; validate identity before persisting any tokens."""
        if attempt.consumed:
            raise SIWCError("ChatGPT sign-in attempt was already used")
        attempt.consumed = True
        try:
            try:
                params = attempt.callback.get(timeout=timeout)
            except queue.Empty as exc:
                raise SIWCError("ChatGPT sign-in timed out") from exc
        finally:
            attempt.close()
        if not hmac.compare_digest(params.get("state", ""), attempt.state):
            raise SIWCError("ChatGPT sign-in state did not match")
        if params.get("error"):
            raise SIWCError("ChatGPT sign-in was declined or cancelled")
        if not params.get("code"):
            raise SIWCError("ChatGPT did not return an authorization code")
        issued_id = params.get("client_id", attempt.client_id)
        if attempt.client_id == "dynamic_agent_client":
            if not issued_id or issued_id == "dynamic_agent_client":
                raise SIWCError("ChatGPT client registration did not complete")
        elif issued_id != attempt.client_id:
            raise SIWCError("ChatGPT returned a different client registration")
        tokens = self._token_request({
            "grant_type": "authorization_code", "client_id": issued_id,
            "code": params["code"], "code_verifier": attempt.verifier,
            "redirect_uri": attempt.redirect_uri, "resource": RESOURCE,
        })
        scopes = set(str(tokens.get("scope", "")).split())
        if REQUIRED_SCOPE not in scopes:
            raise SIWCError("ChatGPT plan usage permission was not granted")
        if not all(isinstance(tokens.get(key), str) and tokens[key]
                   for key in ("id_token", "access_token", "refresh_token")):
            raise SIWCError("ChatGPT did not return a renewable session")
        claims = self._verify_id_token(tokens["id_token"], issued_id, attempt.nonce)
        key = _b64url(hashlib.sha256((issued_id + "\0" + claims["sub"]).encode()).digest())
        with self.store.locked():
            data = self.store.load()
            if attempt.selected_key:
                previous = data["accounts"].get(attempt.selected_key)
                if (not previous or previous["client_id"] != issued_id
                        or previous["subject"] != claims["sub"]):
                    raise SIWCError("ChatGPT account identity changed")
            data["accounts"][key] = {
                "client_id": issued_id, "subject": claims["sub"],
                "issuer": ISSUER, "email": claims.get("email"),
                "host_id": data["host_id"], "id_token": tokens["id_token"],
                "access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
                "scopes": sorted(scopes), "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
            }
            data["active"] = key
            self.store.save(data)
        return key

    def _access_token(self, key: str) -> str:
        with self.store.locked():
            data = self.store.load()
            account = data["accounts"].get(key)
            if not account or REQUIRED_SCOPE not in account.get("scopes", []):
                raise SIWCError("Sign in with ChatGPT and allow plan usage first")
            if account["expires_at"] - time.time() < 120:
                tokens = self._token_request({
                    "grant_type": "refresh_token", "client_id": account["client_id"],
                    "refresh_token": account["refresh_token"], "resource": RESOURCE,
                })
                if not tokens.get("access_token") or not tokens.get("refresh_token"):
                    raise SIWCError("ChatGPT session could not be renewed")
                scopes = set(str(tokens.get("scope", " ".join(account["scopes"]))).split())
                if REQUIRED_SCOPE not in scopes:
                    raise SIWCError("ChatGPT plan usage permission expired")
                account.update(access_token=tokens["access_token"],
                               refresh_token=tokens["refresh_token"], scopes=sorted(scopes),
                               expires_at=time.time() + int(tokens.get("expires_in", 3600)))
                self.store.save(data)
            return account["access_token"]

    def list_models(self, key: str) -> list[dict[str, str]]:
        token = self._access_token(key)
        try:
            response = self.http.get(RESOURCE + "/models", headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            return [{"slug": model["slug"], "display_name": model["display_name"]}
                    for model in response.json()["models"] if model.get("visibility") == "list"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise SIWCError("ChatGPT model list is unavailable") from exc

    def stream_text(self, key: str, model: str, messages: list[dict[str, str]]) -> str:
        """Text only, no tools or actions; caller explicitly chooses a listed model."""
        models = self.list_models(key)
        if model not in {item["slug"] for item in models}:
            raise SIWCError("Select a model available to this ChatGPT account")
        if not messages or any(item.get("role") not in {"developer", "user", "assistant"}
                               or not isinstance(item.get("content"), str) for item in messages):
            raise SIWCError("Conversation history must contain supported text messages")
        token = self._access_token(key)
        request = {"model": model, "input": messages, "store": False, "stream": True}
        chunks: list[str] = []
        completed = False
        try:
            with self.http.stream("POST", RESOURCE + "/responses",
                                  headers={"Authorization": f"Bearer {token}"}, json=request) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[6:])
                    if event.get("type") == "response.output_text.delta":
                        chunks.append(event.get("delta", ""))
                    elif event.get("type") in {"response.failed", "error"}:
                        raise SIWCError("ChatGPT could not complete the reply")
                    elif event.get("type") == "response.completed":
                        completed = True
            if not completed:
                raise SIWCError("ChatGPT reply ended before completion")
            return "".join(chunks)
        except (httpx.HTTPError, ValueError) as exc:
            raise SIWCError("ChatGPT reply is unavailable") from exc
