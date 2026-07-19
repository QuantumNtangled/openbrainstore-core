"""Core operations shared by the MCP server and the CLI. Write path per spec:
validate -> normalize -> write canonical blob -> project row + emit event."""

import io
import tarfile
from collections import Counter

from . import config, store
from .backends.base import Backend
from .canonical import Memory, new_memory_id, utc_now
from .normalize import normalize_entities, normalize_kv, normalize_tags


def _validate_links(backend: Backend, user: str, links: list[str] | None,
                    exclude: str | None = None) -> list[str]:
    """Links are server-resolved relations: every target must be an existing
    memory of the acting tenant. Order-preserving dedupe; self-links dropped."""
    ids = [l for l in dict.fromkeys(links or []) if l and l != exclude]
    if not ids:
        return []
    rows = backend.get_memories(ids)
    missing = [i for i in ids
               if i not in rows or rows[i]["meta"].get("user") != user]
    if missing:
        raise ValueError(f"cannot link to unknown memory ids: {missing}")
    return ids


def remember(
    backend: Backend,
    content: str,
    type: str,
    entities: list[str] | None = None,
    tags: list[str] | None = None,
    kv: dict | None = None,
    links: list[str] | None = None,
    source_harness: str = "mcp",
    user: str | None = None,
) -> dict:
    if not content or not content.strip():
        raise ValueError("content must be non-empty")
    if type not in config.MEMORY_TYPES:
        allowed = ", ".join(sorted(config.MEMORY_TYPES))
        raise ValueError(f"type must be one of: {allowed} (got {type!r})")
    body_bytes = len(content.strip().encode("utf-8"))
    if body_bytes > config.max_body_bytes():
        raise ValueError(
            f"content is {body_bytes} bytes, over the {config.max_body_bytes()}-byte limit "
            "— split this into smaller, more atomic memories"
        )

    user = user or config.user_id()
    backend.set_acting_user(user)
    if backend.count_memories(user) >= config.max_memories():
        raise ValueError(
            f"you have reached the {config.max_memories()}-memory limit for this account "
            "— delete unused memories with forget() or contact support to raise the limit"
        )
    resolved_links = _validate_links(backend, user, links)
    now = utc_now()
    mem = Memory(
        id=new_memory_id(),
        user=user,
        type=type,
        created=now,
        updated=now,
        source_harness=source_harness,
        entities=normalize_entities(entities),
        tags=normalize_tags(tags),
        kv=normalize_kv(kv),
        links=resolved_links,
        body=content.strip(),
    )
    store.write_blob(mem)  # canonical write first: blob is the source of truth
    backend.project_memory(mem)
    backend.emit_event("memory.created", {"id": mem.id, "user": mem.user, "type": mem.type})
    return {
        "id": mem.id,
        "type": mem.type,
        "entities": mem.entities,
        "tags": mem.tags,
        "kv": mem.kv,
        "links": mem.links,
        "created": mem.created,
    }


def link(backend: Backend, id: str, to: list[str], user: str | None = None) -> dict:
    """Add links from an existing memory to related memories. Writes a new
    canonical version (files are immutable; latest version wins) and
    re-projects. read_latest is tenant-pathed, so a foreign id 404s."""
    user = user or config.user_id()
    backend.set_acting_user(user)
    try:
        mem = store.read_latest(user, id)
    except FileNotFoundError:
        raise ValueError(f"memory {id} not found")
    additions = _validate_links(backend, user, to, exclude=id)
    new_links = list(dict.fromkeys([*mem.links, *additions]))
    if new_links == mem.links:
        return {"id": mem.id, "links": mem.links, "updated": mem.updated, "changed": False}
    mem.links = new_links
    mem.updated = utc_now()
    store.write_blob(mem)
    backend.project_memory(mem)
    backend.emit_event("memory.updated", {"id": mem.id, "user": user, "links": new_links})
    return {"id": mem.id, "links": mem.links, "updated": mem.updated, "changed": True}


def forget(backend: Backend, id: str, user: str | None = None) -> dict:
    user = user or config.user_id()
    backend.set_acting_user(user)
    in_projection = bool(backend.get_memories([id]))
    in_blobs = id in store.list_memory_ids(user)
    if not in_projection and not in_blobs:
        raise ValueError(f"memory {id} not found")
    backend.remove_memory(id)
    store.tombstone(user, id)
    backend.emit_event("memory.deleted", {"id": id, "user": user})
    return {"id": id, "status": "tombstoned", "retain_days": config.TOMBSTONE_RETAIN_DAYS}


def get_memory_schema(backend: Backend, user: str | None = None) -> dict:
    """The vocabulary-drift defense: the user's live vocabulary, so agents
    reuse existing keys instead of inventing near-duplicates."""
    user = user or config.user_id()
    backend.set_acting_user(user)
    kv_keys: dict[str, Counter] = {}
    entity_counts: Counter = Counter()
    tag_counts: Counter = Counter()
    type_counts: Counter = Counter()
    total = 0
    for mem_type, meta in backend.iter_meta(user):
        total += 1
        type_counts[mem_type] += 1
        entity_counts.update(meta.get("entities") or [])
        tag_counts.update(meta.get("tags") or [])
        for k, v in (meta.get("kv") or {}).items():
            kv_keys.setdefault(k, Counter())[str(v)] += 1
    return {
        "total_memories": total,
        "types": dict(type_counts),
        "entities": [e for e, _ in entity_counts.most_common(50)],
        "tags": [t for t, _ in tag_counts.most_common(50)],
        "kv_keys": {
            k: {"count": sum(c.values()), "top_values": [v for v, _ in c.most_common(5)]}
            for k, c in sorted(kv_keys.items())
        },
    }


def _add_tar_text(tar: "tarfile.TarFile", name: str, text: str) -> None:
    data = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def export(user: str | None = None, profile: str = "okf") -> dict:
    """Export the user's memories as a tar.gz — the portability promise.

    profile="okf" (default): a clean, browsable Open Knowledge Format bundle —
    one file per memory (latest version), relative Markdown links for the graph,
    index/entity pages, tenant identity stripped (docs/specs/okf-export-profile.md).
    profile="raw": the full-fidelity internal dump (every version, as stored).
    Both stream through the blob store, so filesystem and S3/R2 behave identically."""
    user = user or config.user_id()
    if profile not in ("okf", "raw"):
        raise ValueError("profile must be 'okf' or 'raw'")
    config.export_dir().mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")

    if profile == "raw":
        path = config.export_dir() / f"memories_{user}_{stamp}_raw.tar.gz"
        ids: set[str] = set()
        with tarfile.open(path, "w:gz") as tar:
            for arcname, text in store.export_entries(user):
                _add_tar_text(tar, arcname, text)
                ids.add(arcname.split("/")[1])
        return {"path": str(path), "memories": len(ids), "profile": "raw"}

    from . import okf_export
    memories = [store.read_latest(user, mid) for mid in store.list_memory_ids(user)]
    bundle = okf_export.build_bundle(memories)
    root = f"okf-export-{stamp}"
    path = config.export_dir() / f"{root}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for rel, text in bundle.items():
            _add_tar_text(tar, f"{root}/{rel}", text)
    return {"path": str(path), "memories": len(memories), "profile": "okf"}


def reindex(backend: Backend, user: str | None = None) -> dict:
    """Rebuild every projection from blobs (hard rule #6 made executable)."""
    user = user or config.user_id()
    backend.set_acting_user(user)
    backend.clear_user(user)
    count = 0
    for mem_id in store.list_memory_ids(user):
        backend.project_memory(store.read_latest(user, mem_id))
        count += 1
    return {"reindexed": count}


def stats(backend: Backend, user: str | None = None) -> dict:
    user = user or config.user_id()
    backend.set_acting_user(user)
    return {
        "user": user,
        "backend": config.backend_name(),
        **backend.stats_summary(user),
        "data_dir": str(config.data_dir()),
    }
