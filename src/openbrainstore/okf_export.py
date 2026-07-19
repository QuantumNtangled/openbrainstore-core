"""Build an OKF export bundle (docs/specs/okf-export-profile.md): the portable
interchange form of a memory corpus — one clean Markdown file per memory, the
link graph as relative Markdown links, index/entity pages for navigation, and
tenant identity stripped by default.

Pure: takes Memory objects, returns {bundle_path: text}. Callers (service.py)
handle reading from the store and writing the tar."""

from collections import defaultdict

import yaml

from .canonical import Memory, utc_now

OKF_VERSION = "0.1"


def _first_line(mem: Memory, limit: int = 80) -> str:
    line = (mem.body or "").strip().splitlines()[0] if mem.body.strip() else mem.id
    line = line.strip()
    return (line[: limit - 1] + "…") if len(line) > limit else line


def _md_escape(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def _frontmatter(mem: Memory, include_identity: bool) -> dict:
    fm = {
        "id": mem.id,
        "type": mem.type,
        "created": mem.created,
        "timestamp": mem.updated,  # spec: map internal `updated` -> timestamp
        "entities": list(mem.entities),
        "tags": list(mem.tags),
    }
    obs: dict = {}
    if mem.source_harness and mem.source_harness != "unknown":
        obs["source_harness"] = mem.source_harness
    if mem.kv:
        obs["kv"] = dict(mem.kv)
    if mem.links:
        obs["links"] = list(mem.links)  # raw ids for lossless round-trip
    if include_identity and mem.user:
        obs["user"] = mem.user
    if obs:
        fm["obs"] = obs
    return fm


def _memory_file(mem: Memory, labels: dict[str, str], include_identity: bool) -> str:
    fm = yaml.safe_dump(_frontmatter(mem, include_identity), sort_keys=False,
                        allow_unicode=True).strip()
    parts = [f"---\n{fm}\n---", mem.body.strip()]
    # links -> relative Markdown links to sibling memory files
    related = [l for l in mem.links if l in labels]
    if related:
        lines = "\n".join(
            f"- [{_md_escape(labels[l])}]({l}.md)" for l in related
        )
        parts.append(f"## Related\n{lines}")
    return "\n\n".join(parts).strip() + "\n"


def _index(memories: list[Memory], entities: dict[str, list[str]],
           labels: dict[str, str]) -> str:
    by_type: dict[str, list[Memory]] = defaultdict(list)
    for m in memories:
        by_type[m.type].append(m)

    out = [
        "# OpenBrainStore memory export",
        f"{len(memories)} memories · exported {utc_now()} · "
        f"Open Knowledge Format v{OKF_VERSION}",
        "See `README.md` for the format. Every link below is a relative path — "
        "browse this like a wiki.",
    ]
    out.append("## By type")
    for mtype in sorted(by_type):
        mems = by_type[mtype]
        out.append(f"### {mtype} ({len(mems)})")
        out.append("\n".join(
            f"- [{_md_escape(labels[m.id])}](memories/{m.id}.md)" for m in mems
        ))
    if entities:
        out.append("## By entity")
        out.append("\n".join(
            f"- [{_md_escape(e)} ({len(ids)})](entities/{e}.md)"
            for e, ids in sorted(entities.items())
        ))
    return "\n\n".join(out).strip() + "\n"


def _entity_page(entity: str, ids: list[str], labels: dict[str, str]) -> str:
    lines = "\n".join(f"- [{_md_escape(labels[i])}](../memories/{i}.md)" for i in ids)
    return (
        f"# {entity}\n\nMemories mentioning **{_md_escape(entity)}**.\n\n{lines}\n"
    )


def _readme(count: int) -> str:
    return (
        "# Open Knowledge Format export\n\n"
        f"A portable snapshot of {count} memories in the Open Knowledge Format "
        f"(OKF v{OKF_VERSION}).\n\n"
        "Every memory is a Markdown file with YAML frontmatter under "
        "`memories/`. Start at `index.md`; `entities/` and the by-type groups "
        "provide progressive disclosure. Related memories are ordinary Markdown "
        "links, so the whole bundle browses as a wiki (GitHub, Obsidian, any "
        "Markdown viewer).\n\n"
        "Portable frontmatter: `id`, `type`, `created`, `timestamp`, "
        "`entities`, `tags`. OBS-specific fields (`source_harness`, `kv`, and "
        "the raw link ids) live under the `obs:` key. Tenant identity is "
        "deliberately excluded.\n"
    )


def build_bundle(memories: list[Memory], include_identity: bool = False) -> dict[str, str]:
    """Return {relative_path: text} for the whole OKF bundle."""
    labels = {m.id: _first_line(m) for m in memories}
    entities: dict[str, list[str]] = defaultdict(list)
    for m in memories:
        for e in m.entities:
            entities[e].append(m.id)

    files: dict[str, str] = {
        "README.md": _readme(len(memories)),
        "index.md": _index(memories, entities, labels),
    }
    for m in memories:
        files[f"memories/{m.id}.md"] = _memory_file(m, labels, include_identity)
    for e, ids in entities.items():
        files[f"entities/{e}.md"] = _entity_page(e, ids, labels)
    return files
