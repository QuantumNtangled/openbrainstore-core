"""Canonical memory file: markdown + YAML frontmatter.
The server generates these; clients never submit raw markdown (hard rule #1).
These files are the single source of truth — projections are rebuilt from them."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml
from ulid import ULID


@dataclass
class Memory:
    id: str
    user: str
    type: str
    created: str
    updated: str
    body: str
    source_harness: str = "unknown"
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    kv: dict = field(default_factory=dict)
    links: list[str] = field(default_factory=list)


def new_memory_id() -> str:
    return f"mem_{ULID()}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_markdown(mem: Memory) -> str:
    front = {
        "id": mem.id,
        "user": mem.user,
        "type": mem.type,
        "created": mem.created,
        "updated": mem.updated,
        "source_harness": mem.source_harness,
        "entities": mem.entities,
        "tags": mem.tags,
        "kv": mem.kv,
        "links": mem.links,
    }
    fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{mem.body.strip()}\n"


_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


def from_markdown(text: str) -> Memory:
    m = _FM_RE.match(text)
    if not m:
        raise ValueError("not a canonical memory file (missing frontmatter)")
    front = yaml.safe_load(m.group(1)) or {}
    return Memory(
        id=front["id"],
        user=front["user"],
        type=front["type"],
        created=str(front["created"]),
        updated=str(front["updated"]),
        source_harness=front.get("source_harness", "unknown"),
        entities=list(front.get("entities") or []),
        tags=list(front.get("tags") or []),
        kv=dict(front.get("kv") or {}),
        links=list(front.get("links") or []),
        body=m.group(2).strip(),
    )
