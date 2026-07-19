"""Transport-level rate limiting:
raw ASGI middleware, keyed by bearer token or client IP, fixed 60s window."""

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from openbrainstore.ratelimit import RateLimitMiddleware


async def _ok(request):
    return PlainTextResponse("ok")


@pytest.fixture()
def app():
    a = Starlette(routes=[Route("/ping", _ok)])
    a.add_middleware(RateLimitMiddleware)
    return a


def test_unauthenticated_requests_share_an_ip_bucket(app, monkeypatch):
    from openbrainstore import config
    monkeypatch.setattr(config, "unauth_rate_per_min", lambda: 3)
    client = TestClient(app)
    for _ in range(3):
        assert client.get("/ping").status_code == 200
    res = client.get("/ping")
    assert res.status_code == 429
    assert "Retry-After" in res.headers
    assert res.json()["error"] == "rate_limited"


def test_different_tokens_get_independent_buckets(app, monkeypatch):
    from openbrainstore import config
    monkeypatch.setattr(config, "rate_per_min", lambda: 1)
    client = TestClient(app)
    headers_a = {"Authorization": "Bearer token-a"}
    headers_b = {"Authorization": "Bearer token-b"}
    assert client.get("/ping", headers=headers_a).status_code == 200
    assert client.get("/ping", headers=headers_b).status_code == 200  # separate bucket
    assert client.get("/ping", headers=headers_a).status_code == 429  # a's bucket exhausted


def test_token_and_ip_limits_are_independent_settings(app, monkeypatch):
    from openbrainstore import config
    monkeypatch.setattr(config, "rate_per_min", lambda: 100)
    monkeypatch.setattr(config, "unauth_rate_per_min", lambda: 1)
    client = TestClient(app)
    # unauthenticated: exhausted after 1
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429
    # authenticated: independent, generous limit still open
    assert client.get("/ping", headers={"Authorization": "Bearer t"}).status_code == 200
