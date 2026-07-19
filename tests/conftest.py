import os

import pytest

from openbrainstore import config


def _pg_available() -> bool:
    try:
        # same wake-the-WSL-VM logic the real backend uses
        from openbrainstore.backends.postgres_backend import _connect_with_wsl_wake
        _connect_with_wsl_wake(config.pg_dsn()).close()
        return True
    except Exception:
        return False


_PG_OK = _pg_available()


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path, monkeypatch):
    """Every test runs against both backends; postgres is skipped when no
    server is reachable at OBS_PG_DSN."""
    monkeypatch.setenv("OBS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OBS_USER", "testuser")
    monkeypatch.setenv("OBS_BACKEND", request.param)
    if request.param == "postgres" and not _PG_OK:
        if os.environ.get("OBS_REQUIRE_PG"):
            pytest.fail("OBS_REQUIRE_PG is set but postgres is not reachable")
        pytest.skip("postgres not reachable")
    from openbrainstore.backends import get_backend

    b = get_backend()
    b.set_acting_user("testuser")  # RLS: declare the tenant before touching rows
    b.clear_user("testuser")  # postgres persists across tests; sqlite is fresh per tmp_path
    yield b
    b.set_acting_user("testuser")
    b.clear_user("testuser")
    b.close()
