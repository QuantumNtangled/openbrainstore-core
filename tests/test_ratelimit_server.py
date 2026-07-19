"""Confirms the rate limiter is actually wired into server.main()'s HTTP
startup path under OBS_AUTH=oauth (not just unit-tested in isolation against
a bare Starlette app, and not just asserted via config — a real subprocess,
same as test_http_transport.py's pattern)."""

import http.client
import os
import socket
import subprocess
import sys
import time

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def oauth_http_server(tmp_path):
    port = _free_port()
    env = {
        **os.environ,
        "OBS_DATA_DIR": str(tmp_path / "data"),
        "OBS_USER": "testuser",
        "OBS_BACKEND": "sqlite",
        "OBS_AUTH": "oauth",
        "OBS_ISSUER_URL": f"http://127.0.0.1:{port}",
        "OBS_GH_CLIENT_ID": "fake-client-id",
        "OBS_GH_CLIENT_SECRET": "fake-client-secret",
        "OBS_GITHUB_ALLOWED_USERS": "nobody",
        "OBS_UNAUTH_RATE_PER_MIN": "3",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "openbrainstore.cli", "serve", "--http", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError("server process exited early")
                if time.monotonic() > deadline:
                    raise RuntimeError("server did not start listening in time")
                time.sleep(0.3)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _get(port: int, path: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        return conn.getresponse().status
    finally:
        conn.close()


def test_unauth_rate_limit_enforced_on_real_server(oauth_http_server):
    port = oauth_http_server
    path = "/.well-known/oauth-authorization-server"  # unauthenticated, no GitHub round-trip
    statuses = [_get(port, path) for _ in range(4)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
