"""Postgres backend — the spec's real machinery, per settled decisions:
JSONB + GIN (structured lane), generated tsvector (keyword lane), pg_trgm
similarity for fuzzy kv-key assist, pgvector halfvec + disk-resident HNSW
(vector escape hatch), plain adjacency edges (spec-blessed AGE stand-in,
wrapped here so graph internals never leak to clients).

Local notes: single user, so no RLS; on Windows, a dead connection triggers a
`wsl` poke so the WSL VM (and its postgres service) wakes up."""

import json
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import config
from ..canonical import Memory
from .base import Backend

SCHEMA = """
-- IF NOT EXISTS makes this a no-op notice when the extensions are already
-- installed (e.g. created by a superuser), so a non-superuser role works too.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS memories (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  type        TEXT NOT NULL,
  body        TEXT NOT NULL,
  meta        JSONB NOT NULL,
  fts         TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
  embedding   HALFVEC(384),
  created_at  TIMESTAMPTZ NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_meta ON memories USING GIN (meta jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_mem_fts ON memories USING GIN (fts);
CREATE INDEX IF NOT EXISTS idx_mem_trgm ON memories USING GIN (body gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_mem_user ON memories (user_id, type, created_at);
CREATE INDEX IF NOT EXISTS idx_mem_hnsw ON memories USING hnsw (embedding halfvec_cosine_ops);

CREATE TABLE IF NOT EXISTS edges (
  src     TEXT NOT NULL,
  dst     TEXT NOT NULL,
  rel     TEXT NOT NULL,
  user_id TEXT NOT NULL,
  PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst, user_id);

CREATE TABLE IF NOT EXISTS events (
  id         BIGSERIAL PRIMARY KEY,
  topic      TEXT NOT NULL,
  payload    JSONB NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS recall_log (
  id           BIGSERIAL PRIMARY KEY,
  ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
  lanes        JSONB NOT NULL,
  result_count INTEGER NOT NULL,
  vector_fired BOOLEAN NOT NULL,
  duration_ms  REAL NOT NULL
);

-- Tenant isolation (docs/specs/tenant-isolation.md): RLS as defense in depth.
-- The acting tenant is declared per session via set_config('obs.user_id', ...);
-- current_setting(..., true) is NULL when unset, so policies fail CLOSED.
-- FORCE covers the table-owner case (local dev, fresh installs). The block
-- tolerates a non-owner app role: there, the owner applies this migration
-- once out-of-band (see DEPLOY.md). Superuser connections bypass RLS entirely
-- -- production must connect as the non-superuser obs_app role.
DO $$ BEGIN
  BEGIN
    ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
    ALTER TABLE memories FORCE ROW LEVEL SECURITY;
    ALTER TABLE edges ENABLE ROW LEVEL SECURITY;
    ALTER TABLE edges FORCE ROW LEVEL SECURITY;
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname = 'public' AND tablename = 'memories'
                     AND policyname = 'tenant_isolation') THEN
      CREATE POLICY tenant_isolation ON memories
        USING (user_id = current_setting('obs.user_id', true))
        WITH CHECK (user_id = current_setting('obs.user_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname = 'public' AND tablename = 'edges'
                     AND policyname = 'tenant_isolation') THEN
      CREATE POLICY tenant_isolation ON edges
        USING (user_id = current_setting('obs.user_id', true))
        WITH CHECK (user_id = current_setting('obs.user_id', true));
    END IF;
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;  -- non-owner app role: owner applied the migration out-of-band
  END;
END $$;
"""


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect_with_wsl_wake(dsn: str) -> psycopg.Connection:
    """Connect; on Windows, a refused connection usually means the WSL VM is
    asleep — poke it (which starts systemd and postgres) and retry."""
    try:
        return psycopg.connect(dsn, connect_timeout=5)
    except psycopg.OperationalError:
        if sys.platform != "win32":
            raise
    subprocess.run(["wsl", "-e", "true"], capture_output=True, timeout=60)
    deadline = time.monotonic() + 20
    while True:
        try:
            return psycopg.connect(dsn, connect_timeout=5)
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1.5)


class PostgresBackend(Backend):
    def __init__(self) -> None:
        self.conn = _connect_with_wsl_wake(config.pg_dsn())
        self.conn.row_factory = dict_row
        if self._can_manage_schema():
            with self.conn.cursor() as cur:
                cur.execute(SCHEMA)
            self.conn.commit()

    def _can_manage_schema(self) -> bool:
        """Schema DDL requires table ownership — even CREATE INDEX IF NOT
        EXISTS checks ownership before checking index existence. A non-owner
        app role (prod's obs_app) must skip init entirely; the owner applies
        migrations out-of-band (DEPLOY.md). Fresh database -> we create and
        own everything, so run."""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT (c.relowner = r.oid OR r.rolsuper) AS can_manage
                   FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   CROSS JOIN pg_roles r
                   WHERE c.relname = 'memories' AND n.nspname = 'public'
                     AND r.rolname = current_user""",
            )
            row = cur.fetchone()
        return row is None or bool(row["can_manage"])

    def set_acting_user(self, user: str) -> None:
        # session-scoped (third arg false), so it survives our per-op commits
        with self.conn.cursor() as cur:
            cur.execute("SELECT set_config('obs.user_id', %s, false)", (user,))
        self.conn.commit()

    def _meta(self, mem: Memory) -> dict:
        return {
            "id": mem.id, "user": mem.user, "type": mem.type,
            "created": mem.created, "updated": mem.updated,
            "source_harness": mem.source_harness,
            "entities": mem.entities, "tags": mem.tags,
            "kv": mem.kv, "links": mem.links,
        }

    # ---- write path ----
    def project_memory(self, mem: Memory) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO memories (id, user_id, type, body, meta, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)
                   ON CONFLICT (id) DO UPDATE SET
                     user_id=EXCLUDED.user_id, type=EXCLUDED.type,
                     body=EXCLUDED.body, meta=EXCLUDED.meta,
                     embedding = CASE WHEN memories.body = EXCLUDED.body
                                      THEN memories.embedding ELSE NULL END,
                     created_at=EXCLUDED.created_at, updated_at=EXCLUDED.updated_at""",
                (mem.id, mem.user, mem.type, mem.body, Jsonb(self._meta(mem)),
                 mem.created, mem.updated),
            )
            cur.execute("DELETE FROM edges WHERE src = %s", (mem.id,))
            for ent in mem.entities:
                cur.execute(
                    """INSERT INTO edges (src, dst, rel, user_id)
                       VALUES (%s, %s, 'MENTIONS', %s) ON CONFLICT DO NOTHING""",
                    (mem.id, f"ent:{ent}", mem.user),
                )
            for link in mem.links:
                cur.execute(
                    """INSERT INTO edges (src, dst, rel, user_id)
                       VALUES (%s, %s, 'LINKS_TO', %s) ON CONFLICT DO NOTHING""",
                    (mem.id, link, mem.user),
                )
        self.conn.commit()

    def remove_memory(self, mem_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE id = %s", (mem_id,))
            cur.execute("DELETE FROM edges WHERE src = %s OR dst = %s", (mem_id, mem_id))
        self.conn.commit()

    def emit_event(self, topic: str, payload: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO events (topic, payload, status) VALUES (%s, %s, 'done')",
                (topic, Jsonb(payload)),
            )
        self.conn.commit()

    def clear_user(self, user: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE user_id = %s", (user,))
            cur.execute("DELETE FROM edges WHERE user_id = %s", (user,))
        self.conn.commit()

    # ---- lanes ----
    def _resolve_kv_key(self, user: str, key: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """WITH keys AS (
                     SELECT DISTINCT jsonb_object_keys(meta->'kv') AS k
                     FROM memories WHERE user_id = %s
                   )
                   SELECT k FROM keys
                   WHERE k = %s OR similarity(k, %s) > 0.25
                   ORDER BY (k = %s) DESC, similarity(k, %s) DESC
                   LIMIT 1""",
                (user, key, key, key, key),
            )
            row = cur.fetchone()
        return row["k"] if row else key

    def lane_structured(self, user: str, filters: dict, entities: list[str]) -> list[str]:
        from ..normalize import normalize_tags
        sql = "SELECT id FROM memories WHERE user_id = %s"
        params: list = [user]
        if filters.get("type"):
            sql += " AND type = %s"
            params.append(filters["type"])
        if filters.get("since"):
            sql += " AND created_at >= %s::timestamptz"
            params.append(filters["since"])
        if filters.get("until"):
            sql += " AND created_at <= %s::timestamptz"
            params.append(filters["until"])
        tags = normalize_tags(filters.get("tags"))
        if tags:
            sql += " AND meta->'tags' ?| %s"
            params.append(tags)
        if entities:
            sql += " AND meta->'entities' ?| %s"
            params.append(entities)
        for k, v in (filters.get("kv") or {}).items():
            rk = self._resolve_kv_key(user, k)
            sql += " AND lower(meta #>> %s) = lower(%s)"
            params.extend([["kv", rk], str(v)])
        sql += " ORDER BY created_at DESC"
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [r["id"] for r in cur.fetchall()]

    def lane_fts(self, user: str, query: str, k: int = 50) -> list[str]:
        tokens = re.findall(r"\w+", query)
        if not tokens:
            return []
        tsq = " | ".join(tokens)
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM memories
                   WHERE user_id = %s AND fts @@ to_tsquery('english', %s)
                   ORDER BY ts_rank(fts, to_tsquery('english', %s)) DESC
                   LIMIT %s""",
                (user, tsq, tsq, k),
            )
            return [r["id"] for r in cur.fetchall()]

    def graph_expand(
        self, user: str, entities: list[str], seed_ids: list[str], depth: int
    ) -> list[str]:
        def mentioning(ent_nodes: list[str]) -> list[str]:
            if not ent_nodes:
                return []
            with self.conn.cursor() as cur:
                cur.execute(
                    """SELECT e.src FROM edges e JOIN memories m ON m.id = e.src
                       WHERE e.rel = 'MENTIONS' AND e.user_id = %s AND e.dst = ANY(%s)
                       ORDER BY m.created_at DESC""",
                    (user, ent_nodes),
                )
                return [r["src"] for r in cur.fetchall()]

        def entities_of(mem_ids: list[str]) -> list[str]:
            if not mem_ids:
                return []
            with self.conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT dst FROM edges
                       WHERE rel = 'MENTIONS' AND user_id = %s AND src = ANY(%s)""",
                    (user, mem_ids),
                )
                return [r["dst"] for r in cur.fetchall()]

        def linked(mem_ids: list[str]) -> list[str]:
            if not mem_ids:
                return []
            with self.conn.cursor() as cur:
                cur.execute(
                    """SELECT src, dst FROM edges
                       WHERE rel = 'LINKS_TO' AND user_id = %s
                         AND (src = ANY(%s) OR dst = ANY(%s))""",
                    (user, mem_ids, mem_ids),
                )
                out = []
                for r in cur.fetchall():
                    out.extend([r["src"], r["dst"]])
                return out

        ent_nodes = [f"ent:{e}" for e in entities]
        hop1: list[str] = []
        for mid in mentioning(ent_nodes):
            if mid not in hop1:
                hop1.append(mid)
        for mid in linked(seed_ids):
            if mid not in hop1 and mid not in seed_ids:
                hop1.append(mid)
        result = list(hop1)
        if depth >= 2 and hop1:
            for mid in mentioning(entities_of(hop1)) + linked(hop1):
                if mid not in result and mid not in seed_ids:
                    result.append(mid)
        return result

    # ---- vector storage ----
    @staticmethod
    def _vec_literal(vec: list[float]) -> str:
        return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"

    def pending_embeddings(self, user: str) -> list[tuple[str, str]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, body FROM memories WHERE user_id = %s AND embedding IS NULL",
                (user,),
            )
            return [(r["id"], r["body"]) for r in cur.fetchall()]

    def store_embedding(self, mem_id: str, vec: list[float]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET embedding = %s::halfvec WHERE id = %s",
                (self._vec_literal(vec), mem_id),
            )
        self.conn.commit()

    def vector_search(self, user: str, qvec: list[float], k: int = 20) -> list[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM memories
                   WHERE user_id = %s AND embedding IS NOT NULL
                   ORDER BY embedding <=> %s::halfvec LIMIT %s""",
                (user, self._vec_literal(qvec), k),
            )
            return [r["id"] for r in cur.fetchall()]

    # ---- reads ----
    def get_memories(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, type, body, meta, created_at FROM memories WHERE id = ANY(%s)",
                (ids,),
            )
            return {
                r["id"]: {
                    "id": r["id"], "type": r["type"], "body": r["body"],
                    "meta": r["meta"], "created": _iso(r["created_at"]),
                }
                for r in cur.fetchall()
            }

    def iter_meta(self, user: str) -> Iterator[tuple[str, dict]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT type, meta FROM memories WHERE user_id = %s", (user,))
            for r in cur.fetchall():
                yield r["type"], r["meta"]

    def count_memories(self, user: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM memories WHERE user_id = %s", (user,))
            return cur.fetchone()["n"]

    # ---- instrumentation ----
    def log_recall(self, lanes, result_count, vector_fired, duration_ms) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO recall_log (lanes, result_count, vector_fired, duration_ms)
                   VALUES (%s, %s, %s, %s)""",
                (Jsonb(lanes), result_count, vector_fired, duration_ms),
            )
        self.conn.commit()

    def stats_summary(self, user: str) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE user_id = %s", (user,)
            )
            total = cur.fetchone()["n"]
            cur.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE user_id = %s AND embedding IS NOT NULL",
                (user,),
            )
            embedded = cur.fetchone()["n"]
            cur.execute(
                """SELECT COUNT(*) AS n, COALESCE(AVG(duration_ms), 0) AS avg_ms,
                          COALESCE(SUM(vector_fired::int), 0) AS fires FROM recall_log"""
            )
            r = cur.fetchone()
        return {
            "memories": total, "embedded": embedded, "recalls": r["n"],
            "avg_recall_ms": round(float(r["avg_ms"]), 2),
            "vector_fire_rate": round(r["fires"] / r["n"], 3) if r["n"] else 0.0,
        }

    def close(self) -> None:
        self.conn.close()
