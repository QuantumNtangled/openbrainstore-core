# Spec: Skill catalog

**Status:** shipped. Off by default; turn it on with `OBS_CATALOG`.

## Why

OpenBrainStore stores what agents *know*. The catalog stores how they should *work*:
curated, versioned skills, procedures, guardrails and checklists that any MCP client can
load. A strong model (or a person) distills the guidance once; every agent, including much
cheaper ones, loads the parts that apply to its task.

Agents can **propose** catalog items, but only a human can **promote** one. The catalog is
effectively a remote system prompt, so this keeps it from being poisoned.

## Concepts

- **Item:** a named unit of guidance, identified by a `slug` (`[a-z0-9][a-z0-9-]{1,62}`).
  Each item has a `kind`: `skill`, `procedure`, `guardrail`, `checklist`, `script` or `agent`.
- **Version:** an immutable revision of an item (`v1`, `v2`, …) with a `title`, a
  `when_to_use` trigger (at most 40 words) and a Markdown `body`. Scripts carry their code
  as fenced blocks inside the body; OpenBrainStore never executes them.
- **Status** (per version): `draft` → `promoted` → `retired`, or `rejected`. At most one
  version per item is promoted at a time. Drafts are never served to agents.
- **Ref:** `kind:slug@vN`, for example `skill:release-checklist@v3`. A ref names an exact
  version, so content cannot change mid-task. `get_skill` also accepts a bare `slug`, which
  resolves to the currently promoted version.
- **Session:** one `agent_startup` call. It records mode, task, harness, model and the
  snapshot (a short hash of the refs served), and ties usage and episodes to that bundle.
- **Mode:** `ground` (default) serves guidance only. `optimize` marks an agent-building run
  and is the only mode that accepts `log_episode`.
- **Episode:** the outcome of an optimize run: outcome, optional score, summary and the
  user's corrections. Episodes expire after `OBS_EPISODE_TTL_DAYS` (default 30), are not
  memories, and do not count against the memory quota.
- **Agent profile:** an item of kind `agent` whose body is a role and whose `skills` list
  names the items that role gets. A skill assigned to any profile is shown only to callers
  running as one of those profiles (`agent_startup(agent="reviewer")`); a skill nobody
  assigned is shared with everyone.

## Storage

Canonical files are the source of truth; database rows are disposable projections rebuilt
by `obs reindex`.

```
tenants/{user}/catalog/{slug}/v0001.md     immutable version: YAML frontmatter + body
tenants/{user}/catalog/{slug}/state.json   mutable: promoted, retired and rejected versions, history
```

Projection and instrumentation tables (both backends): `catalog_items`, `catalog_sessions`,
`catalog_usage`, `catalog_episodes`, `catalog_settings`. Only `catalog_items` is rebuilt
from files; sessions, usage and episodes are instrumentation and are dropped on a rebuild,
by design.

**Tenant isolation:** on Postgres every catalog table has the same forced row-level
security policy as `memories` (see [tenant-isolation.md](tenant-isolation.md)), and every
query also filters by `user_id`.

## Turning it on

| `OBS_CATALOG` | effect |
|---|---|
| unset | off: the catalog tools are not registered |
| `1` | opt-in: each account turns it on with `obs catalog enable` (`--user gh_<id>` on a multi-user server). Until then its tools are hidden and calls are refused. |
| `all` | on for every account |

Clients cache their tool list, so reconnect the client after turning the catalog on.

## Telling agents to use it

The server sends no `instructions`, so it never steers the model on its own. When to use
the catalog is your choice, made in your own agent instructions. A starting point for
`CLAUDE.md`, `AGENTS.md` or a system prompt:

```markdown
## OpenBrainStore
- At the start of non-trivial work, call `agent_startup` with a one-line description
  of the task. Fetch the catalog items that apply with `get_skill`, and follow the
  guardrails it returns.
- Before answering questions about past work, decisions or preferences, `recall` them.
- When you learn something durable (a decision, a fact, a preference), `remember` it.
```

## MCP tools

Every tool declares a title and annotations. `agent_startup` and `get_skill` are marked
read-only: they change no item or memory and only record usage. `propose_skill` and
`log_episode` only add.

**`agent_startup(task="", harness="", model="", mode="ground", agent="", format="markdown")`**
returns the session id, inlined guardrails (bodies up to 1,200 characters, at most 5), the
index of promoted items by reference only, and how-to-use notes. The index is capped at
`OBS_CATALOG_INDEX_CAP` entries (default 40); above the cap, entries are ranked by term
overlap with `task`, then by usage. With `agent`, the profile's role and assigned skills
are included.

**`get_skill(refs, session_id="", format="markdown")`** returns the full text of 1-10 refs.
An exact ref resolves if that version is promoted or retired (so pinned sessions keep
working); drafts and unknown refs are listed as missing. Each fetch is recorded against
the session.

**`log_episode(session_id, outcome, summary, corrections="", score=None)`** records how an
optimize-mode run went. The outcome is credited to the versions that session fetched.

**`propose_skill(slug, title, when_to_use, body, kind="skill", evidence=None, session_id="", skills=None)`**
saves the next version of an item as a **draft**. `skills` is only for kind `agent`: an agent
profile's skill list, so a pipeline can propose the profile it wants measured. Agents can
never promote, retire or delete.

Agent-facing tools return Markdown by default (`format="json"` for programmatic clients):
models follow guidance delivered as plain Markdown documents far more reliably than the
same text escaped inside JSON.

## Governance (human-only, CLI)

```
obs catalog import <dir> [--draft]         INDEX.md + one Markdown file per item; promoted unless --draft
obs catalog list [--status s]
obs catalog show <slug|ref>
obs catalog promote <slug> [--version N]   default: newest draft
obs catalog reject <slug> [--version N]    default: newest draft
obs catalog retire <slug>
obs catalog stats                          fetches, sessions and episode outcomes per version
obs catalog episodes [--limit N]
obs catalog enable | disable               per-account switch in opt-in mode
```

`import` reads an `INDEX.md` with lines like
`- release-checklist: Before tagging a release -> CATALOG/release-checklist.md`, plus one
Markdown file per item (optional YAML frontmatter: `kind`, `title`).

Before promoting a changed item, run it against a few held-out tasks: an over-broad
item can make agents apply it where it doesn't belong.

A review pipeline you run yourself, such as a score check on held-out tasks, decides the
same way: it runs these commands, or calls `catalog.promote_version`, `catalog.reject` and
`catalog.retire` from Python with `by=` naming the pipeline, so the item's history records
who decided. There is deliberately no MCP tool for approving. Core has no agent keys to say
which callers may approve, so such a tool would let any connected agent approve its own
proposals.

## Limits

| limit | default | env |
|---|---|---|
| items per tenant | 200 | `OBS_CATALOG_MAX_ITEMS` |
| versions per item | 50 | — |
| body | 16 KB | — |
| index entries served | 40 | `OBS_CATALOG_INDEX_CAP` |
| refs per `get_skill` | 10 | — |
| episode TTL | 30 days | `OBS_EPISODE_TTL_DAYS` |
