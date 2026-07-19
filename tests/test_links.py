"""Memory links: server-resolved relations at write time (remember) and after
the fact (link), persisted in the canonical file, projected as LINKS_TO edges,
and surfaced by the recall graph lane. Runs against both backends."""

import pytest

from openbrainstore import service, store
from openbrainstore.recall import recall


def test_remember_with_links_persists_and_projects(backend):
    a = service.remember(backend, "The base decision.", "decision", user="testuser")
    b = service.remember(
        backend, "A follow-up event revisiting the decision.", "event",
        links=[a["id"]], user="testuser",
    )
    assert b["links"] == [a["id"]]
    # canonical file carries the relation
    mem = store.read_latest("testuser", b["id"])
    assert mem.links == [a["id"]]
    # graph lane: querying the follow-up surfaces the base decision
    out = recall(backend, "testuser", query="follow-up revisiting")
    ids = [r["id"] for r in out["results"]]
    assert b["id"] in ids and a["id"] in ids
    linked_result = next(r for r in out["results"] if r["id"] == b["id"])
    assert linked_result["links"] == [a["id"]]
    assert "graph" in out["lanes_run"]


def test_remember_rejects_unknown_link_target(backend):
    with pytest.raises(ValueError, match="unknown memory ids"):
        service.remember(backend, "x", "fact", links=["mem_DOESNOTEXIST"], user="testuser")


def test_link_after_the_fact_writes_new_version(backend):
    a = service.remember(backend, "First fact.", "fact", user="testuser")
    b = service.remember(backend, "Second fact.", "fact", user="testuser")
    res = service.link(backend, a["id"], [b["id"]], user="testuser")
    assert res["changed"] is True
    assert res["links"] == [b["id"]]
    mem = store.read_latest("testuser", a["id"])
    assert mem.links == [b["id"]]
    assert mem.updated >= mem.created
    # idempotent: linking again changes nothing
    res2 = service.link(backend, a["id"], [b["id"]], user="testuser")
    assert res2["changed"] is False
    # self-links are dropped; unknown targets rejected
    res3 = service.link(backend, a["id"], [a["id"]], user="testuser")
    assert res3["changed"] is False
    with pytest.raises(ValueError, match="unknown memory ids"):
        service.link(backend, a["id"], ["mem_NOPE"], user="testuser")


def test_link_unknown_source_raises(backend):
    with pytest.raises(ValueError, match="not found"):
        service.link(backend, "mem_MISSING", [], user="testuser")


def test_links_are_tenant_scoped(backend):
    """Neither remember(links=) nor link() may reference another tenant's
    memory, and link() cannot modify one."""
    theirs = service.remember(backend, "Someone else's memory.", "fact", user="other-tenant")
    with pytest.raises(ValueError, match="unknown memory ids"):
        service.remember(backend, "mine", "fact", links=[theirs["id"]], user="testuser")
    mine = service.remember(backend, "My memory.", "fact", user="testuser")
    with pytest.raises(ValueError, match="unknown memory ids"):
        service.link(backend, mine["id"], [theirs["id"]], user="testuser")
    with pytest.raises(ValueError, match="not found"):
        service.link(backend, theirs["id"], [mine["id"]], user="testuser")
    # cleanup the extra tenant
    backend.set_acting_user("other-tenant")
    backend.clear_user("other-tenant")
    store.tombstone("other-tenant", theirs["id"])


def test_reindex_preserves_links(backend):
    a = service.remember(backend, "Base.", "fact", user="testuser")
    b = service.remember(backend, "Linked.", "fact", links=[a["id"]], user="testuser")
    backend.set_acting_user("testuser")
    backend.clear_user("testuser")
    service.reindex(backend, user="testuser")
    out = recall(backend, "testuser", query="linked")
    linked = next(r for r in out["results"] if r["id"] == b["id"])
    assert linked["links"] == [a["id"]]
