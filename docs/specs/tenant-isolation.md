# Spec: Tenant isolation (Postgres RLS)

**Status:** approved for build — scheduled **before** the memory-links feature.
**Motivation:** multi-user is now real (the GitHub allowlist can hold more than
one login), and tenancy is currently enforced only by application code.

## Problem

Every query in `PostgresBackend` filters by an explicit `user_id` parameter
threaded from the OAuth token subject. That works, but it is one forgotten
`WHERE user_id = %s` away from a cross-tenant leak — in a recall lane, a graph
expansion, a future feature written in a hurry. The original cloud spec called
for Postgres row-level security "from day one"; the deploy shipped without it.
This spec closes that gap with defense in depth: the database itself refuses to
return another tenant's rows even if the application forgets to ask correctly.

## Design

### 1. Acting-tenant session variable

The backend declares who it is acting for, once per operation:

```sql
SET LOCAL obs.user_id = 'gh_12345678';   -- inside a transaction, or
SELECT set_config('obs.user_id', %s, false);  -- session-scoped per request
```

`Backend` gains a `set_acting_user(user: str)` method; `server.py` calls it
with `_current_user()` at the top of every tool invocation (the CLI likewise).
`SqliteBackend` implements it as a no-op — the SQLite backend is single-user
local by definition.

### 2. Policies on tenant-scoped tables

Applied to `memories` and `edges` (the tables carrying `user_id`):

```sql
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;   -- REQUIRED: obs owns the
                                                 -- tables; owners bypass RLS
                                                 -- unless FORCE is set
CREATE POLICY tenant_isolation ON memories
  USING (user_id = current_setting('obs.user_id', true))
  WITH CHECK (user_id = current_setting('obs.user_id', true));
-- same for edges
```

With `current_setting(..., true)` returning NULL when unset, an operation that
*forgets* to set the acting user sees zero rows and writes nothing — fail
closed, not open.

Not covered by RLS (deliberately): `oauth_clients/states/codes/tokens` (keyed
by opaque secrets, not tenant-queryable), `events` and `recall_log`
(operational, no tenant read path today — revisit if either grows one).

### 3. Keep the explicit filters

The existing `user_id` parameters stay. RLS is the net, not the primary
mechanism — queries remain self-documenting and index-friendly.

## Migration

Schema-init additions (idempotent `DO $$` guards, same pattern as
`CREATE EXTENSION IF NOT EXISTS`). No data changes. Existing single-tenant
data is unaffected. Rollout: deploy, run the leak test, done.

## Acceptance criteria

1. **Leak test (must fail before, pass after):** with two tenants seeded, set
   acting user to tenant A and run a raw `SELECT * FROM memories` with no
   WHERE clause → only A's rows return.
2. **Fail-closed test:** with no acting user set, all reads return empty and
   writes are rejected by `WITH CHECK`.
3. Full existing suite stays green against both backends.
4. Cross-tenant service-level test: authenticate two different token subjects
   over HTTP; each sees only its own memories via `recall` and
   `get_memory_schema`.

## Out of scope

Read-only OAuth scopes (separate, deferred feature), org/shared tenants,
per-tenant physical partitioning (RLS + indexes suffice at this scale, per the
original spec).
