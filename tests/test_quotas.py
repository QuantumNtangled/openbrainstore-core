"""Storage-level quota ceilings:
body size and per-tenant memory count. These are ceilings, not throttles —
normal use must never come close to them."""

import pytest

from openbrainstore import config, service


def test_body_size_ceiling(backend, monkeypatch):
    monkeypatch.setattr(config, "max_body_bytes", lambda: 16)
    service.remember(backend, "fits in 16", "fact", user="testuser")  # exactly 10 bytes, ok
    with pytest.raises(ValueError, match="over the 16-byte limit"):
        service.remember(backend, "this content is definitely over sixteen bytes", "fact",
                         user="testuser")


def test_memory_count_ceiling(backend, monkeypatch):
    monkeypatch.setattr(config, "max_memories", lambda: 2)
    service.remember(backend, "one", "fact", user="testuser")
    service.remember(backend, "two", "fact", user="testuser")
    with pytest.raises(ValueError, match="reached the 2-memory limit"):
        service.remember(backend, "three", "fact", user="testuser")


def test_memory_count_ceiling_is_per_tenant(backend, monkeypatch):
    monkeypatch.setattr(config, "max_memories", lambda: 1)
    service.remember(backend, "tenant a's only slot", "fact", user="testuser")
    # a different tenant has its own, unaffected quota
    service.remember(backend, "tenant b's only slot", "fact", user="other-quota-tenant")
    with pytest.raises(ValueError, match="limit for this account"):
        service.remember(backend, "tenant a over quota", "fact", user="testuser")
    backend.set_acting_user("other-quota-tenant")
    backend.clear_user("other-quota-tenant")
