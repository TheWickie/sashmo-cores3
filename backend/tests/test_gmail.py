from __future__ import annotations

import queue
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from sashmo.gmail import AuthorizationAttempt, GmailClient, GmailError, SCOPE


class MemoryStore:
    def __init__(self, data=None):
        self.data = data

    def load(self):
        return None if self.data is None else dict(self.data)

    def save(self, data):
        self.data = dict(data)

    def forget(self):
        self.data = None


def account():
    return {"client_id": "desktop.apps.googleusercontent.com",
            "access_token": "access", "refresh_token": "refresh", "scope": SCOPE,
            "expires_at": time.time() + 3600}


def test_metadata_poll_is_bounded_and_does_not_request_body_or_search():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer access"
        if request.url.path.endswith("/labels/INBOX"):
            return httpx.Response(200, json={"messagesUnread": 4})
        if request.url.path.endswith("/messages"):
            assert request.url.params["labelIds"] == "INBOX"
            assert request.url.params["maxResults"] == "10"
            assert "q" not in request.url.params
            return httpx.Response(200, json={"messages": [{"id": "abc123"}]})
        assert request.url.params["format"] == "METADATA"
        assert request.url.params.get_list("metadataHeaders") == ["From", "Subject", "Date"]
        return httpx.Response(200, json={"labelIds": ["UNREAD"], "payload": {"headers": [
            {"name": "From", "value": "sender@example.test"},
            {"name": "Subject", "value": "Hello"},
            {"name": "Date", "value": "Fri, 16 Oct 2026 10:00:00 GMT"},
            {"name": "X-Private", "value": "ignore"},
        ]}, "snippet": "Body content must not be stored"})

    client = GmailClient(MemoryStore(account()), httpx.Client(transport=httpx.MockTransport(handle)))
    snapshot = client.poll()
    assert snapshot["unread_count"] == 4
    assert snapshot["messages"] == [{"id": "abc123", "from": "sender@example.test",
                                      "subject": "Hello", "date": "Fri, 16 Oct 2026 10:00:00 GMT",
                                      "unread": True}]
    assert "Body content" not in str(snapshot)
    assert client.poll() == snapshot
    assert len(calls) == 3


def test_refresh_uses_rotated_token_when_returned():
    old = account()
    old["expires_at"] = 0
    forms = []

    def handle(request):
        if request.method == "POST":
            forms.append(request.content.decode())
            return httpx.Response(200, json={"access_token": "new-access",
                                             "refresh_token": "new-refresh", "expires_in": 1800})
        assert request.headers["authorization"] == "Bearer new-access"
        if request.url.path.endswith("INBOX"):
            return httpx.Response(200, json={"messagesUnread": 0})
        return httpx.Response(200, json={"messages": []})

    store = MemoryStore(old)
    client = GmailClient(store, httpx.Client(transport=httpx.MockTransport(handle)))
    assert client.poll()["unread_count"] == 0
    assert store.data["refresh_token"] == "new-refresh"
    assert len(forms) == 1
    assert "grant_type=refresh_token" in forms[0]


class StubServer:
    def __init__(self):
        self.closed = False

    def shutdown(self):
        self.closed = True

    def server_close(self):
        pass


def attempt_with(params):
    callbacks = queue.Queue()
    callbacks.put(params)
    server = StubServer()
    return AuthorizationAttempt("https://accounts.google.com/", "http://127.0.0.1:1234/gmail/callback",
                                "desktop.apps.googleusercontent.com", "expected-state", "verifier",
                                server, callbacks)


def test_oauth_state_rejected_before_token_request():
    attempt = attempt_with({"state": "wrong", "code": "code"})
    client = GmailClient(MemoryStore())
    with pytest.raises(GmailError, match="state did not match"):
        client.finish_authorization(attempt)
    assert attempt.server.closed


def test_oauth_requires_metadata_scope_and_refresh_token():
    def handle(request):
        return httpx.Response(200, json={"scope": "https://www.googleapis.com/auth/gmail.readonly",
                                         "access_token": "access", "refresh_token": "refresh"})
    client = GmailClient(MemoryStore(), httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(GmailError, match="metadata permission"):
        client.finish_authorization(attempt_with({"state": "expected-state", "code": "code"}))
    assert not client.configured()


def test_pkce_and_loopback_parameters():
    client = GmailClient(MemoryStore())
    attempt = client.begin_authorization("desktop.apps.googleusercontent.com")
    try:
        params = parse_qs(urlsplit(attempt.url).query)
        assert params["scope"] == [SCOPE]
        assert params["code_challenge_method"] == ["S256"]
        assert params["state"] == [attempt.state]
        assert params["access_type"] == ["offline"]
        assert params["redirect_uri"] == [attempt.redirect_uri]
        assert attempt.redirect_uri.startswith("http://127.0.0.1:")
        assert "gmail/callback" in attempt.redirect_uri
    finally:
        attempt.close()


def test_gmail_pulse_count_clears_when_refresh_fails():
    from sashmo.state import StateStore

    state = StateStore()
    state.update_gmail({"unread_count": 3, "messages": []}, None)
    connected_version = state.version
    assert state.pulse(-1)["gmail_unread_count"] == 3
    state.update_gmail(None, "Gmail unavailable")
    assert state.version > connected_version
    assert state.pulse(connected_version)["gmail_unread_count"] is None
