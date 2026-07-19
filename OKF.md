# OKF — the Open Knowledge Format

OKF is an open, minimal format for **identity-owned AI knowledge**: the durable
context (facts, decisions, preferences, events, commitments) that a person
accumulates while working with AI systems, stored so that **no harness, model,
or vendor owns it**.

OpenBrainStore is the first OKF service — the memory layer. This document
defines the format and the guarantees any OKF-conformant service must keep.

---

## 1. Principles

1. **Files are the truth.** The canonical unit of knowledge is a plain
   Markdown file with YAML frontmatter. Databases, indexes, embeddings, and
   graphs are disposable projections, rebuildable from the files at any time.
2. **Identity owns the data — never the harness.** Knowledge is scoped to a
   stable person identity (an OAuth token subject), not to the tool, session,
   or vendor that happened to write it. Switching clients must never lose or
   fork the corpus.
3. **Deterministic writes.** No LLM sits in the write path. Normalization
   (key casing, slugification, type coercion) is deterministic, so the same
   input always produces the same file.
4. **Exit is a feature.** Export of the canonical files is available on every
   tier, always. The format works identically on a laptop filesystem, an
   S3-compatible bucket, or a self-hosted server. (Import/re-ingest is
   deliberately out of scope for now; export-only is the current guarantee.)
5. **Open transport.** Services expose the format over open protocols — MCP
   for tools, OAuth 2.1 for identity — never a proprietary SDK.

---

## 2. The canonical file

Every memory is one Markdown file:

```markdown
---
id: mem_01J9XK...            # ULID, server-assigned
user: gh_12345678            # identity subject — the tenant
type: decision               # controlled: fact|decision|preference|event|commitment
created: 2026-07-18T14:02:11Z
updated: 2026-07-18T14:02:11Z
source_harness: claude-code  # informational only, never a scope
entities: [project-alpha, sarah]
tags: [architecture, postgres]
kv:                          # open, normalized key-value bag
  project: okf
  priority: high
links: [mem_01J9XJ...]       # relations to other memories
---

Decided to use warm standby replication instead of Patroni for MVP.
```

### Field rules

| Field | Rule |
|---|---|
| `id` | ULID with `mem_` prefix; server-assigned, immutable |
| `user` | identity subject string; namespaced by provider (`gh_<id>` for GitHub) |
| `type` | controlled vocabulary: `fact`, `decision`, `preference`, `event`, `commitment` |
| `created` / `updated` | UTC ISO 8601, `Z`-suffixed |
| `source_harness` | provenance only — MUST NOT affect scoping or retrieval defaults |
| `entities` / `tags` | lowercase kebab-case slugs, deduplicated |
| `kv` keys | lowercase snake_case; values coerced deterministically (bools, numbers, ISO dates) |
| `links` | ids of related memories |
| body | plain Markdown prose, as supplied by the writer |

### Storage layout

```
tenants/{user}/memories/{id}/{version}.md    # content-addressed versions;
                                             # lexicographic max = latest
tenants/{user}/tombstones/{id}/...           # deleted, retained for a grace
                                             # period, then purged
```

The same key layout applies verbatim to a local directory tree or an
S3-compatible bucket.

---

## 3. The service contract

An OKF memory service exposes, over MCP:

- `remember(content, type, entities?, tags?, kv?, links?)` — validate,
  normalize, write the canonical file, project. Link targets are
  server-resolved: they must be existing memories of the same tenant.
- `link(id, to)` — add relations to an existing memory after the fact; writes
  a new canonical version (files are immutable, latest version wins).
- `recall(query?, filters?, entities?, depth?, deep?)` — layered retrieval:
  structured and full-text lanes first, graph context, vector search only as
  an instrumented escape hatch. Results carry lane provenance.
- `get_memory_schema()` — the tenant's live vocabulary (kv keys, entities,
  tags, type counts), so writers converge on existing terms instead of
  drifting.
- `forget(id)` — remove projections immediately; tombstone the file.
- `export()` — the whole tenant as a portable, browsable OKF bundle: one clean
  Markdown file per memory (latest version), the link graph as relative Markdown
  links, `index.md` + per-entity pages for navigation, tenant identity stripped.
  See `docs/specs/okf-export-profile.md`. (`raw=true` for the full-fidelity
  internal dump.)

Identity: OAuth 2.1 with dynamic client registration and PKCE. The token
subject is the tenant. The harness name is metadata, never a boundary.

---

## 4. Conformance, in one sentence

If you can delete every database and rebuild the service from the Markdown
files alone — and a user can walk away with those files and stand up the same
corpus elsewhere — it's OKF.
