"""End-to-end OAuth 2.1 flow against the real server app (in-process):
discovery -> dynamic client registration -> /authorize (hops to GitHub) ->
/github/callback (faked identity) -> /token with PKCE -> authenticated MCP
call. Plus the denial paths that matter."""

import base64
import hashlib
import importlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

CLIENT_REDIRECT = "http://client.example/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@pytest.fixture()
def oauth_app(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OBS_BACKEND", "sqlite")
    monkeypatch.setenv("OBS_AUTH", "oauth")
    monkeypatch.setenv("OBS_ISSUER_URL", "http://localhost")
    monkeypatch.setenv("OBS_GH_CLIENT_ID", "gh-test-client")
    monkeypatch.setenv("OBS_GH_CLIENT_SECRET", "gh-test-secret")
    monkeypatch.setenv("OBS_GITHUB_ALLOWED_USERS", "allowed-user")
    # TestClient sends Host: testserver; allowlist it (same mechanism prod
    # uses for the public hostname behind Caddy)
    monkeypatch.setenv("OBS_ALLOWED_HOSTS", "testserver")

    import openbrainstore.server as server_mod
    server_mod = importlib.reload(server_mod)

    # fake GitHub: exchange_code returns whoever the test says is logged in
    import openbrainstore.auth.github as gh
    state = {"login": "allowed-user", "id": 4242}

    async def fake_exchange(code, redirect_uri):
        return dict(state)

    monkeypatch.setattr(gh, "exchange_code", fake_exchange)

    app = server_mod.mcp.streamable_http_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, state


def _run_flow_to_token(client, scope="memory") -> dict:
    # 1. discovery
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"].rstrip("/") == "http://localhost"

    # 2. dynamic client registration (what the Claude connector does)
    reg = client.post(
        "/register",
        json={
            "redirect_uris": [CLIENT_REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "test-connector",
        },
    )
    assert reg.status_code in (200, 201), reg.text
    client_id = reg.json()["client_id"]

    # 3. /authorize -> 302 to GitHub with our state
    verifier, challenge = _pkce()
    res = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLIENT_REDIRECT,
            "state": "client-state-xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": scope,
        },
        follow_redirects=False,
    )
    assert res.status_code in (302, 307), res.text
    gh_url = urlparse(res.headers["location"])
    assert gh_url.netloc == "github.com"
    our_state = parse_qs(gh_url.query)["state"][0]

    # 4. GitHub sends the user back to us
    res = client.get(
        "/github/callback",
        params={"code": "gh-code", "state": our_state},
        follow_redirects=False,
    )
    assert res.status_code == 302, res.text
    back = urlparse(res.headers["location"])
    assert back.netloc == "client.example"
    q = parse_qs(back.query)
    assert q["state"] == ["client-state-xyz"]
    code = q["code"][0]

    # 5. token exchange with PKCE
    res = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLIENT_REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert res.status_code == 200, res.text
    return {**res.json(), "client_id": client_id}


def _mcp_initialize(client, token: str | None) -> int:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    res = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}},
        },
    )
    return res.status_code


def test_full_flow_and_authenticated_mcp(oauth_app):
    client, _ = oauth_app
    tok = _run_flow_to_token(client)
    assert tok["access_token"].startswith("obs_at_")
    assert tok["refresh_token"].startswith("obs_rt_")
    assert _mcp_initialize(client, tok["access_token"]) == 200


def test_mcp_rejects_missing_or_bad_token(oauth_app):
    client, _ = oauth_app
    assert _mcp_initialize(client, None) == 401
    assert _mcp_initialize(client, "obs_at_forged") == 401


def test_disallowed_github_user_gets_403(oauth_app):
    client, gh_state = oauth_app
    gh_state["login"] = "intruder"
    gh_state["id"] = 666
    with pytest.raises(AssertionError):
        _run_flow_to_token(client)


def test_refresh_token_rotation(oauth_app):
    client, _ = oauth_app
    tok = _run_flow_to_token(client)
    res = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": tok["client_id"],
        },
    )
    assert res.status_code == 200, res.text
    new = res.json()
    assert new["access_token"] != tok["access_token"]
    # old refresh token was rotated out
    res = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": tok["client_id"],
        },
    )
    assert res.status_code == 400


def test_memory_is_scoped_to_token_subject(oauth_app):
    client, _ = oauth_app
    tok = _run_flow_to_token(client)
    headers = {
        "Authorization": f"Bearer {tok['access_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    # initialize, then write a memory through the authed session
    init = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}})
    assert init.status_code == 200
    session_id = init.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id
    client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized"})
    res = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "remember",
                   "arguments": {"content": "Scoped to my github identity.",
                                  "type": "fact"}}})
    assert res.status_code == 200, res.text
    # blob landed under the token subject's tenant, not the local user
    from openbrainstore import store
    assert store.list_memory_ids("gh_4242"), "memory not scoped to token subject"
    assert not store.list_memory_ids("local")
