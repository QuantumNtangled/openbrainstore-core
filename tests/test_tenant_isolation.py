"""Row-level security acceptance tests (docs/specs/tenant-isolation.md).
Postgres-only: RLS is the database refusing to leak even when application code
forgets a WHERE clause. The sqlite backend is single-user local by definition.

These tests are meaningful only when the connecting role is subject to RLS
(non-superuser, or table owner under FORCE). CI connects as the non-superuser
obs_app role; local dev's obs role owns the tables and FORCE applies."""

import pytest

from openbrainstore import config, service
from openbrainstore.recall import recall

TENANT_A = "iso-tenant-a"
TENANT_B = "iso-tenant-b"


@pytest.fixture()
def pg(backend):
    if config.backend_name() != "postgres":
        pytest.skip("RLS applies to the postgres backend only")
    row = backend.conn.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if row["rolsuper"]:
        pytest.skip("connected as superuser: RLS is bypassed by definition")
    for t in (TENANT_A, TENANT_B):
        backend.set_acting_user(t)
        backend.clear_user(t)
    yield backend
    for t in (TENANT_A, TENANT_B):
        backend.set_acting_user(t)
        backend.clear_user(t)


def _seed_both(pg):
    a = service.remember(pg, "Tenant A's private fact.", "fact", user=TENANT_A)
    b = service.remember(pg, "Tenant B's private fact.", "fact", user=TENANT_B)
    return a, b


def test_filterless_query_cannot_cross_tenants(pg):
    """The leak test: a raw SELECT with no WHERE clause — the exact bug RLS
    exists to catch — must only surface the acting tenant's rows."""
    _seed_both(pg)
    pg.set_acting_user(TENANT_A)
    rows = pg.conn.execute("SELECT user_id FROM memories").fetchall()
    assert {r["user_id"] for r in rows} == {TENANT_A}
    rows = pg.conn.execute("SELECT DISTINCT user_id FROM edges").fetchall()
    assert {r["user_id"] for r in rows} <= {TENANT_A}


def test_no_acting_user_fails_closed(pg):
    """A connection that never declared a tenant sees nothing and writes
    nothing — forgetting set_acting_user is safe, not catastrophic."""
    import psycopg

    _seed_both(pg)
    from openbrainstore.backends import get_backend
    fresh = get_backend()  # separate connection, obs.user_id never set
    try:
        n = fresh.conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"]
        assert n == 0
        with pytest.raises(psycopg.Error):
            fresh.conn.execute(
                """INSERT INTO memories (id, user_id, type, body, meta, created_at, updated_at)
                   VALUES ('mem_rls_probe', %s, 'fact', 'x', '{}', now(), now())""",
                (TENANT_A,),
            )
        fresh.conn.rollback()
    finally:
        fresh.close()


def test_forget_cannot_delete_across_tenants(pg):
    """Knowing another tenant's memory id must not be enough to delete it."""
    a, _ = _seed_both(pg)
    pg.set_acting_user(TENANT_B)
    pg.remove_memory(a["id"])  # RLS silently filters the delete to B's rows
    pg.set_acting_user(TENANT_A)
    assert a["id"] in pg.get_memories([a["id"]])


def test_service_level_isolation(pg):
    _seed_both(pg)
    out = recall(pg, TENANT_A, query="private fact")
    bodies = [r["body"] for r in out["results"]]
    assert any("Tenant A" in b for b in bodies)
    assert not any("Tenant B" in b for b in bodies)
    schema = service.get_memory_schema(pg, user=TENANT_B)
    assert schema["total_memories"] == 1
