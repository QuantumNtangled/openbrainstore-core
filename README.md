# OpenBrainStore

**Identity-owned, long-term memory for AI agents — in an open, portable format.**

OpenBrainStore is a memory service you connect any MCP-speaking AI client to
(Claude, Codex, and others). It gives that client durable, queryable memory
scoped to **your identity**, not to the tool — so your context survives a
switch from one AI client to the next. The canonical data is plain Markdown you
can read, diff, and take with you: the **Open Knowledge Format** ([`OKF.md`](OKF.md)).

The hosted service lives at **[openbrainstore.com](https://openbrainstore.com)**.
This repository is the source you can read and self-host.

> **Source-available, not "open source."** OpenBrainStore is licensed under the
> [PolyForm Small Business License](LICENSE.md): **free to self-host for
> individuals and for companies under $1M/year revenue.** It is published for
> transparency and self-hosting. See [Licensing](#licensing) and
> [Contributing](CONTRIBUTING.md).

## What it is

- **Markdown is the source of truth.** Every memory is a Markdown file with YAML
  frontmatter in object storage. Postgres (or SQLite locally) holds *disposable*
  projections rebuilt from those files — if a projection and a file disagree,
  the file wins.
- **The server generates the file.** Clients call `remember(...)` with
  structured params; the server writes the canonical Markdown. Normalization is
  deterministic — no LLM in the write path.
- **A retrieval cascade, not a black box.** Structured filters and full-text
  search first, a knowledge graph for relational context, and vector search
  only as an instrumented escape hatch. Fused with reciprocal rank fusion; every
  recall is measured.
- **Identity-scoped.** OAuth 2.1 with the token subject as the tenant, enforced
  by Postgres row-level security. Switching clients never switches who you are.
- **Portable by design.** `export` produces a browsable OKF bundle — one file
  per memory, related memories as Markdown links, index and per-entity pages —
  with your identity stripped, so it's safe to share.

## MCP surface

`remember` · `update` · `recall` · `list_memories` · `graph` · `get_memory_schema` ·
`link` · `forget` · `export`

With `OBS_CATALOG` set, a **skill catalog** adds `agent_startup` · `get_skill` ·
`propose_skill` · `log_episode`: curated, versioned guidance (skills, procedures,
checklists, guardrails, agent profiles) that any MCP client loads by reference.
Agents can only *propose* items as drafts; a human promotes them with
`obs catalog promote`. A review pipeline you run yourself (a score check on held-out
tasks, say) approves the same way, through the CLI or `catalog.promote_version` in Python;
no MCP tool approves anything. See [docs/specs/skill-catalog.md](docs/specs/skill-catalog.md).

The tool schema is the contract — everything works from the tool descriptions
alone, because the server doesn't control the calling client. Every tool declares
a title and annotations (read-only, destructive, open-world), and the server sends
no `instructions`: how your agents use OpenBrainStore is up to you. A starting point
for `CLAUDE.md`, `AGENTS.md` or a system prompt:

```markdown
## OpenBrainStore
- At the start of non-trivial work, call `agent_startup` with a one-line description
  of the task. Fetch the catalog items that apply with `get_skill`, and follow the
  guardrails it returns.
- Before answering questions about past work, decisions or preferences, `recall` them.
- When you learn something durable (a decision, a fact, a preference), `remember` it.
```

## Quickstart

Two ways to run it, depending on who needs to reach it:

- **[`LocalSetup.md`](LocalSetup.md)** — your own machine, or a few devices on
  a network you trust. No domain, no TLS, no GitHub OAuth app. Minutes.
- **[`CloudSetup.md`](CloudSetup.md)** — reachable from anywhere, with real
  TLS and GitHub sign-in. Needs a server that's publicly reachable on ports
  80/443 (a cheap VPS; a home machine or WSL box usually isn't, by default)
  and a domain pointed at it. ~15 minutes.

Both run the same code — Docker and a public domain aren't required to use
OpenBrainStore, only to expose it beyond your own machine or LAN.

See [`OKF.md`](OKF.md) for the canonical file format and
[`docs/specs/`](docs/specs) for the design behind tenant isolation and the
export profile.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest          # runs against SQLite and (if reachable) Postgres
```

Backends: `OBS_BACKEND=sqlite` (default, zero-dependency) or `postgres`
(pgvector + pg_trgm). Blob storage: `OBS_BLOB=fs` or `s3` (any S3-compatible
store, incl. Cloudflare R2). To just run the server rather than develop
against it, see [`LocalSetup.md`](LocalSetup.md).

## Licensing

OpenBrainStore is licensed under the
[PolyForm Small Business License 1.0.0](LICENSE.md):

- **Free** for personal use and for companies with **fewer than 100 people and
  under $1M/year revenue**, self-hosting for their own benefit.
- Organizations **over that threshold**, or anyone who wants to **offer
  OpenBrainStore as a hosted service**, need a **commercial license**.

For a commercial license or any licensing question, reach out via the contact
form at **[openbrainstore.com/#contact](https://openbrainstore.com/#contact)**.

## Contributing

The project is public for transparency and self-hosting. We're **not accepting
external code contributions yet** — see [CONTRIBUTING.md](CONTRIBUTING.md).
Security reports are welcome: [SECURITY.md](SECURITY.md).
