from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from sashmo.calendar import CalendarClient, CalendarError, SCOPE


class MemoryStore:
    def __init__(self, data=None):
        self.data = data

    def load(self):
        return None if self.data is None else dict(self.data)

    def save(self, data):
        self.data = dict(data)

    def forget(self):
        self.data = None


def account(expires=3600):
    return {"client_id": "desktop.apps.googleusercontent.com",
            "access_token": "access", "refresh_token": "refresh",
            "scope": SCOPE, "expires_at": time.time() + expires}


def test_authorization_uses_loopback_pkce_and_readonly_scope():
    client = CalendarClient(MemoryStore())
    attempt = client.begin_authorization("desktop.apps.googleusercontent.com")
    try:
        url = urlsplit(attempt.url)
        params = parse_qs(url.query)
        assert url.scheme == "https"
        assert params["scope"] == [SCOPE]
        assert params["code_challenge_method"] == ["S256"]
        assert params["access_type"] == ["offline"]
        assert params["redirect_uri"][0].startswith("http://127.0.0.1:")
        assert attempt.state == params["state"][0]
    finally:
        attempt.close()


def test_authorization_rejects_state_mismatch_before_token_request():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})
    client = CalendarClient(MemoryStore(), httpx.Client(transport=httpx.MockTransport(handler)))
    attempt = client.begin_authorization("desktop.apps.googleusercontent.com")
    attempt.callback.put({"state": "wrong", "code": "secret-code"})
    with pytest.raises(CalendarError, match="state did not match"):
        client.finish_authorization(attempt, timeout=0.1)
    assert not requests


def test_authorization_saves_only_readonly_grant():
    def handler(request):
        assert request.method == "POST"
        body = parse_qs(request.content.decode())
        assert body["grant_type"] == ["authorization_code"]
        assert body["code"] == ["auth-code"]
        assert body["code_verifier"]
        return httpx.Response(200, json={"access_token": "access",
                                         "refresh_token": "refresh",
                                         "scope": SCOPE, "expires_in": 3600})
    store = MemoryStore()
    client = CalendarClient(store, httpx.Client(transport=httpx.MockTransport(handler)))
    attempt = client.begin_authorization("desktop.apps.googleusercontent.com")
    attempt.callback.put({"state": attempt.state, "code": "auth-code"})
    client.finish_authorization(attempt, timeout=0.1)
    assert store.data["scope"] == SCOPE
    assert client.configured()


def test_upcoming_is_bounded_private_and_cached():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer access"
        params = parse_qs(request.url.query.decode())
        assert params["singleEvents"] == ["true"]
        assert params["orderBy"] == ["startTime"]
        assert params["maxResults"] == ["20"]
        assert "description" not in params["fields"][0]
        return httpx.Response(200, json={"items": [
            {"summary": "Dentist", "start": {"dateTime": "2026-10-17T09:00:00+01:00"},
             "end": {"dateTime": "2026-10-17T09:30:00+01:00"}},
            {"visibility": "private", "summary": "Sensitive detail",
             "start": {"date": "2026-10-18"}, "end": {"date": "2026-10-19"}},
            {"status": "cancelled", "summary": "Cancelled",
             "start": {"date": "2026-10-18"}, "end": {"date": "2026-10-19"}},
        ], "nextPageToken": "more"})
    client = CalendarClient(MemoryStore(account()), httpx.Client(transport=httpx.MockTransport(handler)))
    first = client.upcoming(hours=48)
    second = client.upcoming(hours=48)
    assert first == second
    assert len(requests) == 1
    assert [e["title"] for e in first["events"]] == ["Dentist", "Private event"]
    assert first["events"][1]["all_day"] is True
    assert first["truncated"] is True
    assert "Sensitive detail" not in repr(first)


def test_refresh_and_scope_rejection():
    forms = []
    def handler(request):
        if request.method == "POST":
            forms.append(request.content.decode())
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer fresh"
        return httpx.Response(200, json={"items": []})
    store = MemoryStore(account(expires=0))
    client = CalendarClient(store, httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.upcoming()["events"] == []
    assert store.data["access_token"] == "fresh"
    assert len(forms) == 1 and "grant_type=refresh_token" in forms[0]
    store.data["scope"] = "https://www.googleapis.com/auth/calendar"
    with pytest.raises(CalendarError, match="Connect Calendar"):
        client.upcoming(force=True)


def test_forget_clears_local_grant_and_cache():
    store = MemoryStore(account())
    client = CalendarClient(store)
    client._cache = {"hours": 24, "events": []}
    client.forget()
    assert store.load() is None
    assert client._cache is None
