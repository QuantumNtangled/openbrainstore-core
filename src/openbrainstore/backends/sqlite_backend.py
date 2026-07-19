"""SQLite backend — the zero-dependency default. FTS5 keyword lane, JSON meta
structured lane (Python-side predicate filtering; fine at local scale), plain
adjacency graph lane, brute-force cosine vector lane over float16 blobs."""

import difflib
import json
import re
import sqlite3
from collections.abc import Iterator

from .. import config
from ..canonical import Memory, utc_now
from .base import Backend

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  type        TEXT NOT NULL,
  body        TEXT NOT NULL,
  meta        TEXT NOT NULL,
  embedding   BLOB,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_user_type_created
  ON memories (user_id, type, created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  id UNINDEXED, body, entities, tags,
  tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS edges (
  src     TEXT NOT NULL,
  dst     TEXT NOT NULL,
  rel     TEXT NOT NULL,
  user_id TEXT NOT NULL,
  PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst, user_id);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  topic      TEXT NOT NULL,
  payload    TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  claimed_at TEXT
);

CREATE TABLE IF NOT EXISTS recall_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  lanes        TEXT NOT NULL,
  result_count INTEGER NOT NULL,
  vector_fired INTEGER NOT NULL,
  duration_ms  REAL NOT NULL
);
"""


def _meta_json(mem: Memory) -> str:
    return json.dumps(
        {
            "id": mem.id, "user": mem.user, "type": mem.type,
            "created": mem.created, "updated": mem.updated,
            "source_harness": mem.source_harness,
            "entities": mem.entities, "tags": mem.tags,
            "kv": mem.kv, "links": mem.links,
        },
        ensure_ascii=False,
    )


class SqliteBackend(Backend):
    def __init__(self) -> None:
        config.data_dir().mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(config.db_path())
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ---- write path ----
    def project_memory(self, mem: Memory) -> None:
        prev = self.conn.execute(
            "SELECT body, embedding FROM memories WHERE id = ?", (mem.id,)
        ).fetchone()
        embedding = prev["embedding"] if prev and prev["body"] == mem.body else None
        self.conn.execute(
            """INSERT INTO memories (id, user_id, type, body, meta, embedding, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 user_id=excluded.user_id, type=excluded.type, body=excluded.body,
                 meta=excluded.meta, embedding=excluded.embedding,
                 created_at=excluded.created_at, updated_at=excluded.updated_at""",
            (mem.id, mem.user, mem.type, mem.body, _meta_json(mem), embedding,
             mem.created, mem.updated),
        )
        self.conn.execute("DELETE FROM memories_fts WHERE id = ?", (mem.id,))
        self.conn.execute(
            "INSERT INTO memories_fts (id, body, entities, tags) VALUES (?, ?, ?, ?)",
            (mem.id, mem.body, " ".join(mem.entities), " ".join(mem.tags)),
        )
        self.conn.execute("DELETE FROM edges WHERE src = ?", (mem.id,))
        for ent in mem.entities:
            self.conn.execute(
                "INSERT OR IGNORE INTO edges (src, dst, rel, user_id) VALUES (?, ?, 'MENTIONS', ?)",
                (mem.id, f"ent:{ent}", mem.user),
            )
        for link in mem.links:
            self.conn.execute(
                "INSERT OR IGNORE INTO edges (src, dst, rel, user_id) VALUES (?, ?, 'LINKS_TO', ?)",
                (mem.id, link, mem.user),
            )
        self.conn.commit()

    def remove_memory(self, mem_id: str) -> None:
        self.conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        self.conn.execute("DELETE FROM memories_fts WHERE id = ?", (mem_id,))
        self.conn.execute("DELETE FROM edges WHERE src = ? OR dst = ?", (mem_id, mem_id))
        self.conn.commit()

    def emit_event(self, topic: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO events (topic, payload, status, created_at) VALUES (?, ?, 'done', ?)",
            (topic, json.dumps(payload), utc_now()),
        )
        self.conn.commit()

    def clear_user(self, user: str) -> None:
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM memories WHERE user_id = ?", (user,))]
        for mem_id in ids:
            self.conn.execute("DELETE FROM memories_fts WHERE id = ?", (mem_id,))
        self.conn.execute("DELETE FROM memories WHERE user_id = ?", (user,))
        self.conn.execute("DELETE FROM edges WHERE user_id = ?", (user,))
        self.conn.commit()

    # ---- lanes ----
    def _kv_vocab(self, user: str) -> list[str]:
        keys: set[str] = set()
        for row in self.conn.execute("SELECT meta FROM memories WHERE user_id = ?", (user,)):
            keys.update((json.loads(row["meta"]).get("kv") or {}).keys())
        return sorted(keys)

    def _resolve_kv_key(self, user: str, key: str) -> str:
        vocab = self._kv_vocab(user)
        if key in vocab:
            return key
        close = difflib.get_close_matches(key, vocab, n=1, cutoff=0.7)
        return close[0] if close else key

    def lane_structured(self, user: str, filters: dict, entities: list[str]) -> list[str]:
        sql = "SELECT id, meta FROM memories WHERE user_id = ?"
        params: list = [user]
        if filters.get("type"):
            sql += " AND type = ?"
            params.append(filters["type"])
        if filters.get("since"):
            sql += " AND created_at >= ?"
            params.append(filters["since"])
        if filters.get("until"):
            sql += " AND created_at <= ?"
            params.append(filters["until"])
        sql += " ORDER BY created_at DESC"

        from ..normalize import normalize_tags
        want_tags = set(normalize_tags(filters.get("tags")))
        want_entities = set(entities)
        resolved_kv = {
            self._resolve_kv_key(user, k): v for k, v in (filters.get("kv") or {}).items()
        }
        out = []
        for row in self.conn.execute(sql, params):
            meta = json.loads(row["meta"])
            if want_tags and not want_tags & set(meta.get("tags") or []):
                continue
            if want_entities and not want_entities & set(meta.get("entities") or []):
                continue
            kv = meta.get("kv") or {}
            if any(str(kv.get(k)).lower() != str(v).lower() for k, v in resolved_kv.items()):
                continue
            out.append(row["id"])
        return out

    def lane_fts(self, user: str, query: str, k: int = 50) -> list[str]:
        tokens = re.findall(r"\w+", query)
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens)
        rows = self.conn.execute(
            """SELECT f.id FROM memories_fts f
               JOIN memories m ON m.id = f.id
               WHERE memories_fts MATCH ? AND m.user_id = ?
               ORDER BY bm25(memories_fts) LIMIT ?""",
            (match, user, k),
        ).fetchall()
        return [r["id"] for r in rows]

    def graph_expand(
        self, user: str, entities: list[str], seed_ids: list[str], depth: int
    ) -> list[str]:
        def mentioning(ent_nodes: list[str]) -> list[str]:
            if not ent_nodes:
                return []
            ph = ",".join("?" * len(ent_nodes))
            rows = self.conn.execute(
                f"""SELECT e.src FROM edges e JOIN memories m ON m.id = e.src
                    WHERE e.rel = 'MENTIONS' AND e.user_id = ? AND e.dst IN ({ph})
                    ORDER BY m.created_at DESC""",
                [user, *ent_nodes],
            ).fetchall()
            return [r["src"] for r in rows]

        def entities_of(mem_ids: list[str]) -> list[str]:
            if not mem_ids:
                return []
            ph = ",".join("?" * len(mem_ids))
            rows = self.conn.execute(
                f"SELECT DISTINCT dst FROM edges WHERE rel='MENTIONS' AND user_id=? AND src IN ({ph})",
                [user, *mem_ids],
            ).fetchall()
            return [r["dst"] for r in rows]

        def linked(mem_ids: list[str]) -> list[str]:
            if not mem_ids:
                return []
            ph = ",".join("?" * len(mem_ids))
            rows = self.conn.execute(
                f"""SELECT src, dst FROM edges WHERE rel='LINKS_TO' AND user_id=?
                    AND (src IN ({ph}) OR dst IN ({ph}))""",
                [user, *mem_ids, *mem_ids],
            ).fetchall()
            out = []
            for r in rows:
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
    def pending_embeddings(self, user: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT id, body FROM memories WHERE user_id = ? AND embedding IS NULL",
            (user,),
        ).fetchall()
        return [(r["id"], r["body"]) for r in rows]

    def store_embedding(self, mem_id: str, vec: list[float]) -> None:
        import numpy as np
        self.conn.execute(
            "UPDATE memories SET embedding = ? WHERE id = ?",
            (np.asarray(vec, dtype=np.float16).tobytes(), mem_id),
        )
        self.conn.commit()

    def vector_search(self, user: str, qvec: list[float], k: int = 20) -> list[str]:
        import numpy as np
        rows = self.conn.execute(
            "SELECT id, embedding FROM memories WHERE user_id = ? AND embedding IS NOT NULL",
            (user,),
        ).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        mat = np.stack(
            [np.frombuffer(r["embedding"], dtype=np.float16).astype(np.float32) for r in rows]
        )
        sims = mat @ np.asarray(qvec, dtype=np.float32)
        return [ids[i] for i in np.argsort(-sims)[:k]]

    # ---- reads ----
    def get_memories(self, ids: list[str]) -> dict[str, dict]:
        out = {}
        for mem_id in ids:
            row = self.conn.execute(
                "SELECT id, type, body, meta, created_at FROM memories WHERE id = ?",
                (mem_id,),
            ).fetchone()
            if row:
                out[mem_id] = {
                    "id": row["id"], "type": row["type"], "body": row["body"],
                    "meta": json.loads(row["meta"]), "created": row["created_at"],
                }
        return out

    def iter_meta(self, user: str) -> Iterator[tuple[str, dict]]:
        for row in self.conn.execute(
            "SELECT type, meta FROM memories WHERE user_id = ?", (user,)
        ):
            yield row["type"], json.loads(row["meta"])

    def count_memories(self, user: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE user_id = ?", (user,)
        ).fetchone()["n"]

    # ---- instrumentation ----
    def log_recall(self, lanes, result_count, vector_fired, duration_ms) -> None:
        self.conn.execute(
            "INSERT INTO recall_log (ts, lanes, result_count, vector_fired, duration_ms) VALUES (?, ?, ?, ?, ?)",
            (utc_now(), json.dumps(lanes), result_count, int(vector_fired), duration_ms),
        )
        self.conn.commit()

    def stats_summary(self, user: str) -> dict:
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE user_id = ?", (user,)
        ).fetchone()["n"]
        embedded = self.conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE user_id = ? AND embedding IS NOT NULL",
            (user,),
        ).fetchone()["n"]
        r = self.conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(AVG(duration_ms), 0) AS avg_ms,
                      COALESCE(SUM(vector_fired), 0) AS fires FROM recall_log"""
        ).fetchone()
        return {
            "memories": total, "embedded": embedded, "recalls": r["n"],
            "avg_recall_ms": round(r["avg_ms"], 2),
            "vector_fire_rate": round(r["fires"] / r["n"], 3) if r["n"] else 0.0,
        }

    def close(self) -> None:
        self.conn.close()
