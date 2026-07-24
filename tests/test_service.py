import tarfile

import pytest

from openbrainstore import service, store
from openbrainstore.recall import recall


def seed(backend):
    a = service.remember(
        backend,
        "Decided to use warm standby replication instead of Patroni for the MVP.",
        "decision",
        entities=["okf", "postgres"],
        tags=["architecture", "availability"],
        kv={"Project": "OKF", "priority": "high"},
    )
    b = service.remember(
        backend,
        "Sarah prefers code reviews before noon on weekdays.",
        "preference",
        entities=["sarah"],
        tags=["workflow"],
    )
    c = service.remember(
        backend,
        "Shipped the pgBackRest restore drill; PITR to a scratch box passed.",
        "event",
        entities=["okf", "postgres"],
        tags=["backups"],
        kv={"project": "okf"},
    )
    return a, b, c


def test_remember_normalizes_and_persists_blob(backend):
    res = service.remember(
        backend, "Test fact.", "fact",
        entities=["Project Alpha"], tags=["Big Idea"], kv={"Due Date": "01/15/2026"},
    )
    assert res["entities"] == ["project-alpha"]
    assert res["tags"] == ["big-idea"]
    assert res["kv"] == {"due_date": "2026-01-15"}
    mem = store.read_latest("testuser", res["id"])
    assert mem.body == "Test fact."
    assert mem.kv["due_date"] == "2026-01-15"


def test_remember_rejects_bad_type(backend):
    with pytest.raises(ValueError, match="type must be one of"):
        service.remember(backend, "x", "musing")


def test_structured_recall(backend):
    a, _, c = seed(backend)
    out = recall(backend, "testuser", filters={"type": "decision"})
    assert [r["id"] for r in out["results"]] == [a["id"]]

    out = recall(backend, "testuser", filters={"kv": {"project": "okf"}})
    ids = {r["id"] for r in out["results"]}
    assert {a["id"], c["id"]} <= ids


def test_fuzzy_kv_key_assist(backend):
    a, _, _ = seed(backend)
    # near-miss key "projekt" should fuzzy-match "project"
    out = recall(backend, "testuser", filters={"kv": {"projekt": "okf"}, "type": "decision"})
    assert [r["id"] for r in out["results"]] == [a["id"]]


def test_fts_recall(backend):
    a, b, _ = seed(backend)
    out = recall(backend, "testuser", query="replication standby")
    assert out["results"][0]["id"] == a["id"]
    out = recall(backend, "testuser", query="code review preferences")
    assert b["id"] in [r["id"] for r in out["results"]]


def test_sort_browse_newest_and_oldest(backend):
    a, b, c = seed(backend)
    # no query, no filters + recency sort = browse latest
    out = recall(backend, "testuser", sort="newest")
    assert [r["id"] for r in out["results"]] == [c["id"], b["id"], a["id"]]
    assert "browse" in out["lanes_run"]

    out = recall(backend, "testuser", sort="oldest", limit=1)
    assert [r["id"] for r in out["results"]] == [a["id"]]

    # last-memory idiom
    out = recall(backend, "testuser", sort="newest", limit=1)
    assert [r["id"] for r in out["results"]] == [c["id"]]


def test_sort_newest_with_query(backend):
    a, _, c = seed(backend)
    out = recall(backend, "testuser", filters={"kv": {"project": "okf"}}, sort="newest")
    ids = [r["id"] for r in out["results"]]
    assert ids.index(c["id"]) < ids.index(a["id"])


def test_sort_rejects_unknown_order(backend):
    with pytest.raises(ValueError, match="sort must be one of"):
        recall(backend, "testuser", sort="sideways")


def test_date_bounds_apply_to_all_lanes(backend):
    seed(backend)
    # fts would match, but the window excludes everything
    out = recall(
        backend, "testuser", query="replication standby",
        filters={"until": "2000-01-01"},
    )
    assert out["results"] == []
    out = recall(
        backend, "testuser", query="replication standby",
        filters={"since": "2000-01-01"},
    )
    assert out["results"] != []


def test_graph_expansion(backend):
    a, _, c = seed(backend)
    out = recall(backend, "testuser", entities=["postgres"])
    ids = [r["id"] for r in out["results"]]
    assert a["id"] in ids and c["id"] in ids
    assert "graph" in out["lanes_run"]


def test_schema_vocabulary(backend):
    seed(backend)
    schema = service.get_memory_schema(backend)
    assert schema["total_memories"] == 3
    assert schema["types"] == {"decision": 1, "preference": 1, "event": 1}
    assert "project" in schema["kv_keys"]
    assert "okf" in [v.lower() for v in schema["kv_keys"]["project"]["top_values"]]
    assert "sarah" in schema["entities"]


def test_update_content_reprojects(backend):
    a, _, _ = seed(backend)
    out = service.update(backend, a["id"], content="Switched to Patroni after the beta scaling review.")
    assert out["changed"] == ["content"]
    # canonical blob revised
    assert store.read_latest("testuser", a["id"]).body.startswith("Switched to Patroni")
    # new words findable via fts; old words no longer LEXICALLY match it
    # (the semantic lane may still surface it by meaning — that's correct)
    hits = recall(backend, "testuser", query="Patroni scaling review")
    assert hits["results"][0]["id"] == a["id"]
    old = recall(backend, "testuser", query="warm standby replication")
    assert all(r["id"] != a["id"] or "fts" not in r["lanes"] for r in old["results"])


def test_update_metadata_only_preserves_body(backend):
    a, _, _ = seed(backend)
    out = service.update(backend, a["id"], tags=["Architecture", "Revisited"])
    assert out["changed"] == ["tags"]
    assert out["tags"] == ["architecture", "revisited"]  # normalized
    row = backend.get_memories([a["id"]])[a["id"]]
    assert "warm standby" in row["body"]                  # body untouched
    assert out["created"] == a["created"]                 # created preserved


def test_update_embedding_invalidation(backend):
    pytest.importorskip("numpy")  # store_embedding needs the [vector] extra
    a, _, _ = seed(backend)
    backend.store_embedding(a["id"], [0.1] * 384)
    assert a["id"] not in [i for i, _ in backend.pending_embeddings("testuser")]
    # metadata-only update keeps the embedding
    service.update(backend, a["id"], tags=["revisited"])
    assert a["id"] not in [i for i, _ in backend.pending_embeddings("testuser")]
    # body change drops it for re-embed (checked WITHOUT recalling: the
    # vector lane self-heals pending embeddings during search)
    service.update(backend, a["id"], content="Entirely new body text.")
    assert a["id"] in [i for i, _ in backend.pending_embeddings("testuser")]


def test_update_entities_rebuild_graph_edges(backend):
    a, _, _ = seed(backend)
    service.update(backend, a["id"], entities=["timescale"])
    ids = [r["id"] for r in recall(backend, "testuser", entities=["timescale"])["results"]]
    assert a["id"] in ids
    assert a["id"] not in [
        r["id"] for r in recall(backend, "testuser", entities=["postgres"])["results"]
    ]


def test_update_links_replace_not_append(backend):
    a, b, c = seed(backend)
    service.link(backend, a["id"], [b["id"]])
    out = service.update(backend, a["id"], links=[c["id"]])
    assert out["links"] == [c["id"]]
    out = service.update(backend, a["id"], links=[])
    assert out["links"] == []
    with pytest.raises(ValueError, match="unknown memory ids"):
        service.update(backend, a["id"], links=["mem_nope"])


def test_update_noop_and_validation(backend):
    a, _, _ = seed(backend)
    assert service.update(backend, a["id"])["changed"] == []
    assert service.update(backend, a["id"], type="decision")["changed"] == []  # same value
    with pytest.raises(ValueError, match="not found"):
        service.update(backend, a["id"] + "x", content="y")
    with pytest.raises(ValueError, match="type must be one of"):
        service.update(backend, a["id"], type="musing")
    with pytest.raises(ValueError, match="non-empty"):
        service.update(backend, a["id"], content="   ")


def test_forget_tombstones(backend):
    a, _, _ = seed(backend)
    service.forget(backend, a["id"])
    out = recall(backend, "testuser", filters={"type": "decision"})
    assert out["results"] == []
    assert a["id"] not in store.list_memory_ids("testuser")
    assert store.is_tombstoned("testuser", a["id"])
    with pytest.raises(ValueError):
        service.forget(backend, a["id"] + "x")


def test_export_tar(backend):
    seed(backend)
    res = service.export()
    assert res["memories"] == 3 and res["profile"] == "okf"
    with tarfile.open(res["path"]) as tar:
        names = tar.getnames()
    # OKF bundle: one file per memory under memories/, plus index/README/entities
    assert sum("/memories/" in n and n.endswith(".md") for n in names) == 3
    assert any(n.endswith("/index.md") for n in names)


def test_reindex_rebuilds_from_blobs(backend):
    seed(backend)
    before = recall(backend, "testuser", query="replication standby")
    backend.clear_user("testuser")  # nuke projections, then rebuild from canonical files
    assert recall(backend, "testuser", filters={"type": "decision"})["results"] == []
    res = service.reindex(backend)
    assert res["reindexed"] == 3
    after = recall(backend, "testuser", query="replication standby")
    assert [r["id"] for r in after["results"]] == [r["id"] for r in before["results"]]


def test_recall_is_instrumented(backend):
    seed(backend)
    recall(backend, "testuser", query="replication")
    stats = service.stats(backend)
    assert stats["recalls"] >= 1
    assert stats["memories"] == 3
