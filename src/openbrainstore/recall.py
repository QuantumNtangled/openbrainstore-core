"""Retrieval cascade + reciprocal rank fusion, backend-agnostic.

Lane order matters (spec section 4): structured and full-text are primary,
graph adds relational context, vector fires only on fallthrough or deep=true.
Every recall is instrumented — recall failures are silent, so metrics are the
only way to see them."""

import time
from datetime import datetime, timezone

from . import config, embeddings
from .backends.base import Backend
from .normalize import normalize_entities

SORT_ORDERS = ("relevance", "newest", "oldest")


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _within(created: str, since: str | None, until: str | None) -> bool:
    ts = _parse_ts(created)
    if since and ts < _parse_ts(since):
        return False
    if until and ts > _parse_ts(until):
        return False
    return True


def _rrf(lanes: dict[str, list[str]]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranked in lanes.values():
        for rank, mem_id in enumerate(ranked):
            scores[mem_id] = scores.get(mem_id, 0.0) + 1.0 / (config.RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def recall(
    backend: Backend,
    user: str,
    query: str | None = None,
    filters: dict | None = None,
    entities: list[str] | None = None,
    depth: int = 1,
    deep: bool = False,
    limit: int = config.DEFAULT_RECALL_LIMIT,
    sort: str = "relevance",
) -> dict:
    t0 = time.perf_counter()
    if sort not in SORT_ORDERS:
        raise ValueError(f"sort must be one of {', '.join(SORT_ORDERS)}")
    backend.set_acting_user(user)
    filters = filters or {}
    entities = normalize_entities(entities)
    lanes: dict[str, list[str]] = {}

    # Lanes 1-2: primary
    if filters or entities:
        lanes["structured"] = backend.lane_structured(user, filters, entities)
    if query:
        lanes["fts"] = backend.lane_fts(user, query)

    # Browse mode: an explicit recency sort with nothing to match against is a
    # request for "my latest memories", not an empty result.
    if not lanes and sort != "relevance":
        lanes["browse"] = backend.lane_structured(user, {}, [])

    # Lane 3: graph context from matched entities + top primary hits
    seeds = []
    for ranked in lanes.values():
        seeds.extend(ranked[:5])
    if entities or seeds:
        lanes["graph"] = backend.graph_expand(
            user, entities, seeds, depth=max(1, min(depth, 2))
        )

    fused = _rrf(lanes)

    # Escape hatch: fires on fallthrough or explicit deep=true, never by default
    vector_fired = False
    if query and embeddings.available() and (
        deep or len(fused) < config.MIN_RESULTS_BEFORE_VECTOR
    ):
        vector_fired = True
        lanes["vector"] = embeddings.search(backend, user, query)
        fused = _rrf(lanes)

    # since/until must bound EVERY lane, not just structured — fts/graph/vector
    # hits outside the window are dropped here. Date bounds and recency sorts
    # need the full matched set before the limit cut; the plain relevance path
    # keeps the cheap top-N fetch.
    since, until = filters.get("since"), filters.get("until")
    bounded = bool(since or until)
    top = fused if (bounded or sort != "relevance") else fused[:limit]
    rows = backend.get_memories([mem_id for mem_id, _ in top])
    results = []
    for mem_id, score in top:
        row = rows.get(mem_id)
        if not row:
            continue
        if bounded and not _within(row["created"], since, until):
            continue
        meta = row["meta"]
        results.append(
            {
                "id": row["id"],
                "type": row["type"],
                "score": round(score, 5),
                "body": row["body"],
                "entities": meta.get("entities") or [],
                "tags": meta.get("tags") or [],
                "kv": meta.get("kv") or {},
                "links": meta.get("links") or [],
                "created": row["created"],
                "lanes": [name for name, ranked in lanes.items() if mem_id in ranked],
            }
        )

    if sort != "relevance":
        # created has second precision, so ties are common; ULID ids are
        # monotonic and break them in true creation order.
        results.sort(key=lambda r: (r["created"], r["id"]), reverse=(sort == "newest"))
    results = results[:limit]

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    backend.log_recall(
        {name: len(r) for name, r in lanes.items()}, len(results), vector_fired, duration_ms
    )
    return {
        "results": results,
        "lanes_run": {name: len(r) for name, r in lanes.items()},
        "vector_fired": vector_fired,
        "duration_ms": duration_ms,
    }
