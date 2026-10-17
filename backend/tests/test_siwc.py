"""Offline protocol tests: no browser sign-in and no inference request to OpenAI."""

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from sashmo.siwc import ISSUER, RESOURCE, SIWCClient, SIWCError, WindowsTokenStore


@pytest.fixture
def signed_client(tmp_path: Path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "test-key"
    observed = []
    nonce = {"value": ""}

    def handler(request: httpx.Request):
        observed.append(request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "jwks_uri": ISSUER + "/.well-known/jwks.json",
            })
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [public_jwk]})
        if request.url.path == "/api/accounts/oauth/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                assert form["client_id"] == ["oaiapp_example"]
                assert form["resource"] == [RESOURCE]
                assert "scope" not in form
                return httpx.Response(200, json={
                    "access_token": "refreshed-access", "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                })
            assert form["client_id"] == ["oaiapp_example"]
            assert form["code_verifier"][0]
            assert form["redirect_uri"][0].startswith("http://127.0.0.1:")
            claims = {"iss": ISSUER, "aud": "oaiapp_example", "sub": "test-subject",
                      "email": "sample@example.test", "nonce": nonce["value"],
                      "iat": int(time.time()), "exp": int(time.time()) + 3600}
            return httpx.Response(200, json={
                "id_token": jwt.encode(claims, private_key, algorithm="RS256",
                                       headers={"kid": "test-key"}),
                "access_token": "test-access", "refresh_token": "test-refresh",
                "scope": "openid offline_access chatgpt.tokens.use.direct resource.invoke",
                "expires_in": 3600,
            })
        if request.url.path == "/v1/models":
            assert request.headers["authorization"] in {"Bearer test-access", "Bearer refreshed-access"}
            return httpx.Response(200, json={"models": [
                {"slug": "allowed", "display_name": "Allowed", "visibility": "list"},
                {"slug": "hidden", "display_name": "Hidden", "visibility": "hidden"},
            ]})
        if request.url.path == "/v1/responses":
            assert request.headers["authorization"] in {"Bearer test-access", "Bearer refreshed-access"}
            body = json.loads(request.content)
            assert body == {"model": "allowed", "input": [{"role": "user", "content": "Hello"}],
                            "store": False, "stream": True}
            return httpx.Response(200, text=(
                'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
                'data: {"type":"response.completed"}\n\n'
            ), headers={"content-type": "text/event-stream"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    store = WindowsTokenStore(tmp_path / "credentials.bin")
    client = SIWCClient(store, httpx.Client(transport=httpx.MockTransport(handler)))
    return client, store, observed, nonce


def test_registration_id_token_storage_and_streaming(signed_client):
    client, store, observed, nonce = signed_client
    attempt = client.begin_authorization()
    parameters = parse_qs(urlsplit(attempt.url).query)
    assert parameters["client_id"] == ["dynamic_agent_client"]
    assert parameters["agent_name_hint"] == ["Sashmo"]
    assert parameters["code_challenge_method"] == ["S256"]
    assert parameters["scope"][0].find("chatgpt.tokens.use.direct") >= 0
    nonce["value"] = parameters["nonce"][0]
    callback = attempt.redirect_uri + "?code=test-code&client_id=oaiapp_example&state=" + attempt.state
    assert httpx.get(callback).status_code == 200
    key = client.finish_authorization(attempt)
    assert store.path.read_bytes()[:1] != b"{"  # DPAPI ciphertext, not plaintext tokens.
    data = store.load()
    assert data["active"] == key
    assert data["accounts"][key]["subject"] == "test-subject"
    assert client.list_models(key) == [{"slug": "allowed", "display_name": "Allowed"}]
    assert client.active_selection() is None
    client.select_model(key, "allowed")
    assert client.active_selection() == (key, "allowed")
    assert client.account_summaries()[0]["model"] == "allowed"
    assert client.routed_model(key, "allowed", True) == "allowed"
    client.configure_auto_model(key, True, "allowed")
    assert client.account_summaries()[0]["auto_model"] is True
    assert client.routed_model(key, "base", False) == "base"
    assert client.routed_model(key, "base", True) == "allowed"
    with pytest.raises(SIWCError, match="available model"):
        client.configure_auto_model(key, True, "hidden")
    client.configure_auto_model(key, False, None)
    assert client.routed_model(key, "base", True) == "base"
    assert "access_token" not in client.account_summaries()[0]
    with pytest.raises(SIWCError, match="Select a model"):
        client.stream_text(key, "hidden", [{"role": "user", "content": "Hello"}])
    assert client.stream_text(key, "allowed", [{"role": "user", "content": "Hello"}]) == "Hi"
    with pytest.raises(SIWCError, match="already used"):
        client.finish_authorization(attempt, timeout=0)
    assert len([request for request in observed if request.url.path == "/v1/responses"]) == 1
    client.forget_account(key)
    assert client.active_selection() is None
    assert client.account_summaries() == []


def test_state_mismatch_never_exchanges_code(signed_client):
    client, store, observed, _ = signed_client
    attempt = client.begin_authorization()
    assert httpx.get(attempt.redirect_uri + "?code=test-code&client_id=oaiapp_example&state=wrong").status_code == 200
    with pytest.raises(SIWCError, match="state"):
        client.finish_authorization(attempt)
    assert not any(request.url.path == "/api/accounts/oauth/token" for request in observed)
    assert not store.load()["accounts"]


def test_refresh_rotates_both_tokens_under_store_lock(signed_client):
    client, store, _, nonce = signed_client
    attempt = client.begin_authorization()
    nonce["value"] = attempt.nonce
    httpx.get(attempt.redirect_uri + "?code=test-code&client_id=oaiapp_example&state=" + attempt.state)
    key = client.finish_authorization(attempt)
    with store.locked():
        data = store.load()
        data["accounts"][key]["expires_at"] = time.time() - 1
        store.save(data)
    assert client._access_token(key) == "refreshed-access"
    account = store.load()["accounts"][key]
    assert account["access_token"] == "refreshed-access"
    assert account["refresh_token"] == "rotated-refresh"
