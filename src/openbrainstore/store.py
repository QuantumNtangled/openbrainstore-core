"""Canonical blob operations, backend-agnostic. Same key layout as the spec:
tenants/{user}/memories/{id}/{version}.md, content-addressed versions.
Version filenames sort lexicographically so the latest key is max().
The underlying store is filesystem (default) or S3-compatible (OBS_BLOB=s3)."""

import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone, timedelta

from . import config
from .blobstore import get_blobstore
from .canonical import Memory, to_markdown, from_markdown


def _mem_prefix(user: str, mem_id: str) -> str:
    return f"tenants/{user}/memories/{mem_id}/"


def _tomb_prefix(user: str, mem_id: str) -> str:
    return f"tenants/{user}/tombstones/{mem_id}/"


def write_blob(mem: Memory) -> str:
    content = to_markdown(mem)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    # microsecond-resolution write stamp (not mem.updated, which is
    # second-resolution): two versions written within the same second must
    # still sort in write order, or read_latest ties on the random digest
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    key = _mem_prefix(mem.user, mem.id) + f"{stamp}_{digest}.md"
    get_blobstore().put_text(key, content)
    return key


def read_latest(user: str, mem_id: str) -> Memory:
    bs = get_blobstore()
    keys = [k for k in bs.list_keys(_mem_prefix(user, mem_id)) if k.endswith(".md")]
    if not keys:
        raise FileNotFoundError(f"no versions for {mem_id}")
    return from_markdown(bs.get_text(keys[-1]))


def list_memory_ids(user: str) -> list[str]:
    prefix = f"tenants/{user}/memories/"
    ids = {k[len(prefix):].split("/", 1)[0] for k in get_blobstore().list_keys(prefix)}
    return sorted(ids)


def tombstone(user: str, mem_id: str) -> None:
    """Move the memory's blobs to tombstones (retain N days, then purge)."""
    bs = get_blobstore()
    src = _mem_prefix(user, mem_id)
    dst = _tomb_prefix(user, mem_id)
    keys = bs.list_keys(src)
    if not keys:
        return
    for key in keys:
        bs.copy(key, dst + key[len(src):])
        bs.delete(key)
    bs.put_text(dst + "_deleted_at", datetime.now(timezone.utc).isoformat())


def is_tombstoned(user: str, mem_id: str) -> bool:
    return bool(get_blobstore().list_keys(_tomb_prefix(user, mem_id)))


def purge_tombstones(user: str, retain_days: int | None = None) -> int:
    retain = retain_days if retain_days is not None else config.TOMBSTONE_RETAIN_DAYS
    bs = get_blobstore()
    prefix = f"tenants/{user}/tombstones/"
    by_id: dict[str, list[str]] = {}
    for key in bs.list_keys(prefix):
        mem_id = key[len(prefix):].split("/", 1)[0]
        by_id.setdefault(mem_id, []).append(key)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retain)
    purged = 0
    for mem_id, keys in by_id.items():
        marker = prefix + mem_id + "/_deleted_at"
        if marker not in keys:
            continue
        try:
            deleted_at = datetime.fromisoformat(bs.get_text(marker).strip())
        except ValueError:
            continue
        if deleted_at < cutoff:
            for key in keys:
                bs.delete(key)
            purged += 1
    return purged


def export_entries(user: str) -> Iterator[tuple[str, str]]:
    """(archive_name, content) for every canonical file — feeds export()."""
    bs = get_blobstore()
    prefix = f"tenants/{user}/memories/"
    for key in bs.list_keys(prefix):
        yield "memories/" + key[len(prefix):], bs.get_text(key)
