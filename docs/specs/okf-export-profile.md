# Spec: OKF export profile

**Status:** approved for build. Realizes the OKF portability promise — "your
memory is markdown you can take with you" — as a clean, browsable bundle
rather than a raw internal dump. Builds on the earlier design decision
(memory id `mem_01KXW4RP…`) and the format defined in `OKF.md`.

## Why

`export()` today tars the internal canonical files verbatim:
`memories/{id}/{version}.md` — every historical version, the tenant subject
(`user: gh_12345678`) stamped in each file, `source_harness`, and `links` as a
raw id array. That's faithful for migration, but it's neither portable (leaks
your identity) nor pleasant to read or navigate.

The OKF profile is the *interchange* format: one clean file per memory, the
graph expressed as Markdown links, identity stripped, and index pages so a
human or an agent can walk the corpus. It becomes the default; the raw dump
stays behind a flag.

## Bundle layout

```
okf-export-<timestamp>/
  README.md               # what this is, OKF version, counts
  index.md                # entry point: memories grouped by type + entity links
  memories/
    <id>.md               # one file per memory, latest version only
  entities/
    <entity>.md           # every memory mentioning <entity>, linked
```

Progressive disclosure: `index.md` → an entity page (or a type group) →
individual memory files. Every cross-reference is a relative Markdown link, so
the bundle is a self-contained wiki that renders on GitHub, in Obsidian, or any
Markdown viewer.

## Memory file (`memories/<id>.md`)

```markdown
---
id: mem_01J9XK...
type: decision
created: 2026-07-18T14:02:11Z
timestamp: 2026-07-18T14:02:11Z     # mapped from the internal `updated`
entities: [project-alpha, sarah]
tags: [architecture, postgres]
obs:                                # OBS-specific extension, namespaced
  source_harness: claude-code
  kv:
    project: okf
    priority: high
  links: [mem_01J9XJ...]            # raw ids, for lossless round-trip
---

Decided to use warm standby replication instead of Patroni for MVP.

## Related
- [Shipped the pgBackRest restore drill…](mem_01J9XJ....md)
```

Rules:
- **Latest version only** (`read_latest`); the version-stamped internal
  filenames and history are dropped.
- **Portable core** frontmatter: `id`, `type`, `created`, `timestamp`,
  `entities`, `tags`. `timestamp` is the internal `updated` (last change).
- **`obs:` namespace** holds everything OBS-specific: `source_harness`, `kv`,
  and the raw `links` ids (so a future importer can reconstruct the graph
  exactly, even though the human-facing form is the Markdown links).
- **Tenant identity excluded by default.** No `user` / `gh_<id>` anywhere. An
  `include_identity=True` option may re-add it under `obs:` for the owner's own
  archival use, but the default export is safe to share.
- **Related section**: for each linked memory, a Markdown link to `<id>.md`
  (same directory, relative) with the target's first body line as the label.

## Index & entity pages

- `index.md`: title, count, generated date, OKF version; memories grouped under
  a heading per `type`, each a bullet linking to `memories/<id>.md`; then an
  "By entity" list linking to each `entities/<entity>.md`.
- `entities/<entity>.md`: heading = entity; every memory mentioning it, linked
  via `../memories/<id>.md`.
- Link labels are the target memory's first body line, trimmed (fallback: id).

Entity names are already slugified (lowercase kebab), so they're safe file
names.

## Surface

- `service.export(user, profile="okf" | "raw")` — default `okf`. `raw`
  preserves today's behavior (full-fidelity internal tar).
- MCP `export()` tool: default OKF bundle; `raw: bool = False` for the internal
  dump. Tool description updated to describe the browsable bundle.
- CLI: `obs export` (OKF) / `obs export --raw`.

## Non-goals

Import/re-ingest (still out of scope per OKF.md — this is export-only). Version
history in the OKF bundle (backups cover fidelity; the profile is about the
current state). Cross-tenant or multi-user bundles.

## Acceptance criteria

1. `export(profile="okf")` produces a tar whose `memories/<id>.md` files carry
   the portable frontmatter, `obs:` namespace, and **no tenant identity**.
2. `links` render as working relative Markdown links under `## Related`, and
   the raw ids survive under `obs.links`.
3. `index.md` groups by type and links every memory; `entities/<e>.md` exists
   for each entity and links its memories; `README.md` documents the format.
4. Only the latest version of each memory appears; count matches the corpus.
5. `export(profile="raw")` is byte-for-byte the current behavior.
6. Tests cover bundle construction from `Memory` objects (no DB) plus an
   end-to-end `service.export` through a backend.
