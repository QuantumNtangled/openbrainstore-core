"""Skill catalog: curated, versioned guidance served to agents by reference
(docs/specs/skill-catalog.md).

Canonical versions live as blobs (tenants/{user}/catalog/{slug}/vNNNN.md plus a
mutable state.json); catalog_items is a projection rebuilt by reindex().
Sessions, usage and episodes are instrumentation only: database rows, never
blobs. Episodes expire, and they are accepted only from optimize-mode sessions.
Agents may propose drafts; promotion, retirement and import are human-only and
reachable from the CLI, never from MCP."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from ulid import ULID

from . import config
from .backends.base import Backend
from .blobstore import get_blobstore
from .canonical import utc_now

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
REF_RE = re.compile(r"^(?:(?P<kind>[a-z]+):)?(?P<slug>[a-z0-9][a-z0-9-]{1,62})(?:@v(?P<version>\d+))?$")
MAX_BODY_BYTES = 16 * 1024
MAX_VERSIONS = 50
MAX_TRIGGER_WORDS = 40
MAX_TITLE_CHARS = 120
MAX_REFS_PER_FETCH = 10
MAX_EPISODE_TEXT = 8 * 1024
GUARDRAIL_INLINE_CHARS = 1200
GUARDRAIL_INLINE_MAX = 5
MODES = ("ground", "optimize")


# ---------------------------------------------------------------- schema

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_items (
  user_id TEXT NOT NULL, slug TEXT NOT NULL, version INTEGER NOT NULL,
  kind TEXT NOT NULL, title TEXT NOT NULL, when_to_use TEXT NOT NULL, body TEXT NOT NULL,
  status TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  skills TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (user_id, slug, version)
);
CREATE TABLE IF NOT EXISTS catalog_sessions (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mode TEXT NOT NULL, task TEXT NOT NULL,
  harness TEXT NOT NULL, model TEXT NOT NULL, snapshot TEXT NOT NULL, refs TEXT NOT NULL,
  created_at TEXT NOT NULL, agent TEXT NOT NULL DEFAULT '', key_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS catalog_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
  slug TEXT NOT NULL, version INTEGER NOT NULL, fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cat_usage ON catalog_usage (user_id, session_id);
CREATE TABLE IF NOT EXISTS catalog_episodes (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL, outcome TEXT NOT NULL,
  score REAL, summary TEXT NOT NULL, corrections TEXT NOT NULL, refs_used TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cat_episodes ON catalog_episodes (user_id, created_at);
CREATE TABLE IF NOT EXISTS catalog_settings (
  user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL
);
"""

# columns added after the catalog first shipped; older databases gain them on startup
_MIGRATIONS = (("catalog_items", "skills", "TEXT NOT NULL DEFAULT '[]'"),
               ("catalog_sessions", "agent", "TEXT NOT NULL DEFAULT ''"),
               ("catalog_sessions", "key_id", "TEXT NOT NULL DEFAULT ''"))

_PG_TABLES = ("catalog_items", "catalog_sessions", "catalog_usage", "catalog_episodes",
              "catalog_settings")

_PG_SCHEMA = _SQLITE_SCHEMA.replace(
    "id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGSERIAL PRIMARY KEY"
) + "".join(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} {d};\n" for t, c, d in _MIGRATIONS) + """
DO $$ BEGIN
  BEGIN
""" + "".join(f"""
    ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname = 'public' AND tablename = '{t}'
                     AND policyname = 'tenant_isolation') THEN
      CREATE POLICY tenant_isolation ON {t}
        USING (user_id = current_setting('obs.user_id', true))
        WITH CHECK (user_id = current_setting('obs.user_id', true));
    END IF;""" for t in _PG_TABLES) + """
  EXCEPTION WHEN insufficient_privilege THEN
    NULL;  -- non-owner app role: owner applied the migration out-of-band
  END;
END $$;
"""

_initialized: set[str] = set()


class _Db:
    """Dialect shim over the backend's own connection, so the backend's
    acting-user setting (and therefore Postgres RLS) applies to catalog rows."""

    def __init__(self, backend: Backend):
        self.conn = backend.conn
        self.pg = config.backend_name() == "postgres"
        key = (config.pg_dsn() if self.pg else str(config.db_path()))
        if key not in _initialized:
            self._init_schema()
            _initialized.add(key)

    def _init_schema(self) -> None:
        if not self.pg:
            self.conn.executescript(_SQLITE_SCHEMA)
            for table, col, ddl in _MIGRATIONS:
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            self.conn.commit()
            return
        import psycopg
        try:
            with self.conn.cursor() as cur:
                cur.execute(_PG_SCHEMA)
            self.conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            self.conn.rollback()  # non-owner role: the table owner applies the schema out of band

    def run(self, sql: str, params: tuple = ()) -> list[dict]:
        if self.pg:
            with self.conn.cursor() as cur:
                cur.execute(sql.replace("?", "%s"), params)
                rows = cur.fetchall() if cur.description else []
            return [dict(r) for r in rows]
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []

    def commit(self) -> None:
        self.conn.commit()


def _db(backend: Backend, user: str) -> _Db:
    backend.set_acting_user(user)
    return _Db(backend)


# ---------------------------------------------------------------- canonical blobs

@dataclass
class Version:
    slug: str
    version: int
    kind: str
    title: str
    when_to_use: str
    body: str
    created: str
    created_by: str
    session_id: str = ""
    evidence: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)  # agent profiles only

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.slug}@v{self.version}"


def _item_prefix(user: str, slug: str) -> str:
    return f"tenants/{user}/catalog/{slug}/"


def _version_key(user: str, slug: str, version: int) -> str:
    return _item_prefix(user, slug) + f"v{version:04d}.md"


def _state_key(user: str, slug: str) -> str:
    return _item_prefix(user, slug) + "state.json"


def _version_to_text(v: Version) -> str:
    front = {
        "slug": v.slug, "version": v.version, "kind": v.kind, "title": v.title,
        "when_to_use": v.when_to_use, "created": v.created, "created_by": v.created_by,
        "session_id": v.session_id, "evidence": v.evidence,
    }
    if v.kind == "agent":
        front["skills"] = v.skills
    fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{v.body.strip()}\n"


_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


def _version_from_text(text: str) -> Version:
    m = _FM_RE.match(text)
    if not m:
        raise ValueError("not a catalog version file (missing frontmatter)")
    f = yaml.safe_load(m.group(1)) or {}
    return Version(
        slug=f["slug"], version=int(f["version"]), kind=f["kind"], title=f["title"],
        when_to_use=f["when_to_use"], body=m.group(2).strip(), created=str(f["created"]),
        created_by=f.get("created_by", "unknown"), session_id=f.get("session_id") or "",
        evidence=list(f.get("evidence") or []), skills=list(f.get("skills") or []),
    )


def _read_state(user: str, slug: str) -> dict:
    bs = get_blobstore()
    key = _state_key(user, slug)
    if key not in bs.list_keys(_item_prefix(user, slug)):
        return {"promoted": None, "retired": [], "history": []}
    return json.loads(bs.get_text(key))


def _write_state(user: str, slug: str, state: dict) -> None:
    state["history"] = state.get("history", [])[-200:]
    get_blobstore().put_text(_state_key(user, slug), json.dumps(state, indent=2))


def _list_versions(user: str, slug: str) -> list[int]:
    out = []
    for key in get_blobstore().list_keys(_item_prefix(user, slug)):
        m = re.search(r"/v(\d{4})\.md$", key)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def _list_slugs(user: str) -> list[str]:
    prefix = f"tenants/{user}/catalog/"
    return sorted({k[len(prefix):].split("/", 1)[0] for k in get_blobstore().list_keys(prefix)})


def _status(state: dict, version: int) -> str:
    if state.get("promoted") == version:
        return "promoted"
    if version in state.get("retired", []):
        return "retired"
    if version in state.get("rejected", []):
        return "rejected"
    return "draft"


# ---------------------------------------------------------------- projection

def _project_item(db: _Db, user: str, slug: str) -> None:
    """Re-project every version of one item from blobs (status from state.json)."""
    state = _read_state(user, slug)
    bs = get_blobstore()
    db.run("DELETE FROM catalog_items WHERE user_id = ? AND slug = ?", (user, slug))
    for n in _list_versions(user, slug):
        v = _version_from_text(bs.get_text(_version_key(user, slug, n)))
        db.run(
            "INSERT INTO catalog_items (user_id, slug, version, kind, title, when_to_use, body, "
            "status, created_by, created_at, skills) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user, v.slug, v.version, v.kind, v.title, v.when_to_use, v.body,
             _status(state, n), v.created_by, v.created, json.dumps(v.skills)),
        )
    db.commit()


def reindex(backend: Backend, user: str | None = None) -> dict:
    """Rebuild catalog_items from canonical blobs (hard rule #6)."""
    user = user or config.user_id()
    db = _db(backend, user)
    db.run("DELETE FROM catalog_items WHERE user_id = ?", (user,))
    db.commit()
    slugs = _list_slugs(user)
    for slug in slugs:
        _project_item(db, user, slug)
    return {"catalog_items": len(slugs)}


def clear_user(backend: Backend, user: str) -> None:
    """Drop every catalog projection and instrumentation row for a user (tests)."""
    db = _db(backend, user)
    for t in _PG_TABLES:
        db.run(f"DELETE FROM {t} WHERE user_id = ?", (user,))
    db.commit()


# ---------------------------------------------------------------- validation

def _validate(slug: str, kind: str, title: str, when_to_use: str, body: str) -> None:
    if not SLUG_RE.match(slug or ""):
        raise ValueError(f"slug must match {SLUG_RE.pattern} (lowercase letters, digits, hyphens)")
    if kind not in config.CATALOG_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(config.CATALOG_KINDS))}")
    if not title.strip() or len(title.strip()) > MAX_TITLE_CHARS:
        raise ValueError(f"title must be 1-{MAX_TITLE_CHARS} characters")
    words = len(when_to_use.split())
    if words == 0 or words > MAX_TRIGGER_WORDS:
        raise ValueError(f"when_to_use must be 1-{MAX_TRIGGER_WORDS} words (got {words})")
    size = len(body.strip().encode("utf-8"))
    if size == 0 or size > MAX_BODY_BYTES:
        raise ValueError(f"body must be 1-{MAX_BODY_BYTES} bytes (got {size})")


def parse_ref(ref: str) -> tuple[str, int | None]:
    m = REF_RE.match((ref or "").strip())
    if not m:
        raise ValueError(f"bad ref {ref!r}: expected kind:slug@vN or a bare slug")
    return m.group("slug"), int(m.group("version")) if m.group("version") else None


# ---------------------------------------------------------------- write path

def add_version(backend: Backend, user: str, slug: str, title: str, when_to_use: str, body: str,
                kind: str = "skill", created_by: str = "human:cli", session_id: str = "",
                evidence: list[str] | None = None, promote: bool = False,
                skills: list[str] | None = None) -> dict:
    """Append the next version of an item (draft unless promote=True). Agent profiles
    carry `skills`: the slugs of the items that agent gets."""
    title, when_to_use, body = title.strip(), " ".join(when_to_use.split()), body.strip()
    _validate(slug, kind, title, when_to_use, body)
    skills = list(dict.fromkeys(skills or []))
    if skills and kind != "agent":
        raise ValueError("only agent profiles have a skills list")
    if len(skills) > 100 or any(not SLUG_RE.match(x or "") or x == slug for x in skills):
        raise ValueError("skills must be up to 100 catalog item slugs (not the profile itself)")
    db = _db(backend, user)
    existing = _list_versions(user, slug)
    if not existing and len(_list_slugs(user)) >= config.catalog_max_items():
        raise ValueError(f"catalog is at its {config.catalog_max_items()}-item limit")
    if len(existing) >= MAX_VERSIONS:
        raise ValueError(f"{slug} already has {MAX_VERSIONS} versions; retire or consolidate first")
    n = (existing[-1] if existing else 0) + 1
    v = Version(slug=slug, version=n, kind=kind, title=title, when_to_use=when_to_use, body=body,
                created=utc_now(), created_by=created_by, session_id=session_id or "",
                evidence=list(dict.fromkeys(evidence or [])), skills=skills)
    get_blobstore().put_text(_version_key(user, slug, n), _version_to_text(v))
    state = _read_state(user, slug)
    state.setdefault("history", []).append({"ts": v.created, "action": "add", "version": n, "by": created_by})
    _write_state(user, slug, state)
    if promote:
        return promote_version(backend, user, slug, n, by=created_by)
    _project_item(db, user, slug)
    return {"ref": v.ref, "status": "draft"}


def propose(backend: Backend, user: str, slug: str, title: str, when_to_use: str, body: str,
            kind: str = "skill", evidence: list[str] | None = None, session_id: str = "",
            actor: str = "", skills: list[str] | None = None) -> dict:
    """Agent path: always a draft. A human promotes it (or not) from the CLI."""
    harness = "mcp"
    if session_id:
        rows = _db(backend, user).run(
            "SELECT harness FROM catalog_sessions WHERE id = ? AND user_id = ?", (session_id, user))
        if not rows:
            raise ValueError(f"unknown session {session_id}")
        harness = rows[0]["harness"] or "mcp"
    out = add_version(backend, user, slug, title, when_to_use, body, kind=kind,
                      created_by=f"agent:{actor or harness}", session_id=session_id, evidence=evidence,
                      skills=skills)
    out["note"] = "Saved as a draft. Drafts are never served; a human must review and promote it."
    return out


def promote_version(backend: Backend, user: str, slug: str, version: int | None = None,
                    by: str = "human:cli") -> dict:
    versions = _list_versions(user, slug)
    if not versions:
        raise ValueError(f"no catalog item named {slug}")
    state = _read_state(user, slug)
    if version is None:
        drafts = [n for n in versions if _status(state, n) == "draft"]
        if not drafts:
            raise ValueError(f"{slug} has no draft to promote")
        version = drafts[-1]
    if version not in versions:
        raise ValueError(f"{slug} has no v{version}")
    prev = state.get("promoted")
    if prev and prev != version and prev not in state.setdefault("retired", []):
        state["retired"].append(prev)  # pinned sessions keep resolving the old version
    state["retired"] = [n for n in state.get("retired", []) if n != version]
    state["rejected"] = [n for n in state.get("rejected", []) if n != version]
    state["promoted"] = version
    state.setdefault("history", []).append({"ts": utc_now(), "action": "promote", "version": version, "by": by})
    _write_state(user, slug, state)
    _project_item(_db(backend, user), user, slug)
    v = _version_from_text(get_blobstore().get_text(_version_key(user, slug, version)))
    return {"ref": v.ref, "status": "promoted", "replaced": f"v{prev}" if prev and prev != version else None}


def retire(backend: Backend, user: str, slug: str, by: str = "human:cli") -> dict:
    state = _read_state(user, slug)
    current = state.get("promoted")
    if not current:
        raise ValueError(f"{slug} has no promoted version")
    state["promoted"] = None
    state.setdefault("retired", []).append(current)
    state.setdefault("history", []).append({"ts": utc_now(), "action": "retire", "version": current, "by": by})
    _write_state(user, slug, state)
    _project_item(_db(backend, user), user, slug)
    return {"slug": slug, "retired": f"v{current}"}


def reject(backend: Backend, user: str, slug: str, version: int | None = None,
           by: str = "human:cli") -> dict:
    """Decline a draft. Kept in history, never served; promoting it later undoes this."""
    versions = _list_versions(user, slug)
    if not versions:
        raise ValueError(f"no catalog item named {slug}")
    state = _read_state(user, slug)
    if version is None:
        drafts = [n for n in versions if _status(state, n) == "draft"]
        if not drafts:
            raise ValueError(f"{slug} has no draft to reject")
        version = drafts[-1]
    if version not in versions:
        raise ValueError(f"{slug} has no v{version}")
    if _status(state, version) != "draft":
        raise ValueError(f"{slug} v{version} is {_status(state, version)}, not a draft")
    state.setdefault("rejected", []).append(version)
    state.setdefault("history", []).append({"ts": utc_now(), "action": "reject", "version": version, "by": by})
    _write_state(user, slug, state)
    _project_item(_db(backend, user), user, slug)
    return {"slug": slug, "rejected": f"v{version}"}


def account_enabled(backend: Backend, user: str) -> bool:
    """Whether this account has the catalog on. 'all' mode: always. Otherwise the
    account's explicit choice; with no choice recorded, accounts that already have
    catalog items count as on (so existing catalogs keep working) and new ones as off."""
    mode = config.catalog_mode()
    if mode == "off":
        return False
    if mode == "all":
        return True
    db = _db(backend, user)
    rows = db.run("SELECT enabled FROM catalog_settings WHERE user_id = ?", (user,))
    if rows:
        return bool(rows[0]["enabled"])
    return bool(db.run("SELECT 1 AS ok FROM catalog_items WHERE user_id = ? LIMIT 1", (user,)))


def set_account_enabled(backend: Backend, user: str, enabled: bool) -> dict:
    db = _db(backend, user)
    db.run("DELETE FROM catalog_settings WHERE user_id = ?", (user,))
    db.run("INSERT INTO catalog_settings (user_id, enabled, updated_at) VALUES (?, ?, ?)",
           (user, 1 if enabled else 0, utc_now()))
    db.commit()
    return {"enabled": bool(enabled)}


_INDEX_LINE = re.compile(r"^\s*[-*]\s*([a-z0-9][a-z0-9-]+)\s*:\s*(.+?)\s*->\s*(\S+)\s*$")


def _infer_kind(slug: str) -> str:
    for word, kind in (("checklist", "checklist"), ("guardrail", "guardrail"),
                       ("workflow", "procedure"), ("procedure", "procedure"), ("script", "script")):
        if word in slug:
            return kind
    return "skill"


def import_dir(backend: Backend, user: str, path: str, promote: bool = True) -> dict:
    """Import a study-format catalog: INDEX.md lines '- slug: when to use -> X/slug.md'
    plus one Markdown file per item (optional YAML frontmatter: kind, title)."""
    root = Path(path)
    index = root / "INDEX.md"
    if not index.exists():
        raise ValueError(f"{index} not found")
    out = []
    for line in index.read_text(encoding="utf-8").splitlines():
        m = _INDEX_LINE.match(line)
        if not m:
            continue
        slug, when, target = m.group(1), m.group(2), m.group(3)
        item_path = root / Path(target).name
        text = item_path.read_text(encoding="utf-8")
        front, body = {}, text
        fm = _FM_RE.match(text)
        if fm:
            front, body = yaml.safe_load(fm.group(1)) or {}, fm.group(2)
        title = front.get("title")
        if not title:
            heading = next((l for l in body.splitlines() if l.startswith("# ")), None)
            title = heading[2:].strip() if heading else slug
            if heading:
                body = body.replace(heading, "", 1)
        out.append(add_version(
            backend, user, slug, title, when, body, kind=front.get("kind") or _infer_kind(slug),
            created_by=f"import:{root.name}", promote=promote))
    if not out:
        raise ValueError(f"no index lines found in {index}")
    return {"imported": len(out), "items": out}


# ---------------------------------------------------------------- read path (agents)

def _promoted(db: _Db, user: str) -> list[dict]:
    return db.run(
        "SELECT slug, version, kind, title, when_to_use, body, skills FROM catalog_items "
        "WHERE user_id = ? AND status = 'promoted' ORDER BY slug", (user,))


def _profiles(rows: list[dict]) -> dict[str, dict]:
    """Promoted agent profiles: slug -> row with a parsed skill set."""
    out = {}
    for r in rows:
        if r["kind"] == "agent":
            out[r["slug"]] = dict(r, skill_set=set(json.loads(r.get("skills") or "[]")))
    return out


def _visible(slug: str, agent: str, profiles: dict[str, dict]) -> bool:
    """Profiles restrict: a skill assigned to any profile is visible only to those
    profiles; a skill nobody assigned is shared with every caller."""
    holders = [p for p, row in profiles.items() if slug in row["skill_set"]]
    return not holders or agent in holders


def _ref(row: dict) -> str:
    return f"{row['kind']}:{row['slug']}@v{row['version']}"


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3}


def startup(backend: Backend, user: str, task: str = "", harness: str = "", model: str = "",
            mode: str = "ground", agent: str = "", key_id: str = "") -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    db = _db(backend, user)
    all_rows = _promoted(db, user)
    profiles = _profiles(all_rows)
    agent = parse_ref(agent)[0] if agent else ""
    role = profiles.get(agent)
    if agent and role is None:
        raise ValueError(f"no live agent profile named {agent!r}")
    rows = [r for r in all_rows if r["kind"] != "agent" and _visible(r["slug"], agent, profiles)]
    guard_rows = [r for r in rows if r["kind"] == "guardrail"][:GUARDRAIL_INLINE_MAX]
    rest = [r for r in rows if r not in guard_rows]
    cap = config.catalog_index_cap()
    omitted = 0
    if len(rest) > cap:
        usage = {(u["slug"], u["version"]): u["n"] for u in db.run(
            "SELECT slug, version, COUNT(*) AS n FROM catalog_usage WHERE user_id = ? "
            "GROUP BY slug, version", (user,))}
        want = _terms(task)
        rest.sort(key=lambda r: (-len(want & _terms(r["title"] + " " + r["when_to_use"])),
                                 -usage.get((r["slug"], r["version"]), 0), r["slug"]))
        omitted = len(rest) - cap
        rest = rest[:cap]
    refs = sorted([_ref(r) for r in guard_rows + rest] + ([_ref(role)] if role else []))
    snapshot = "cat_" + hashlib.sha256("\n".join(refs).encode()).hexdigest()[:8]
    session_id = f"ses_{ULID()}"
    db.run(
        "INSERT INTO catalog_sessions (id, user_id, mode, task, harness, model, snapshot, refs, "
        "created_at, agent, key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, user, mode, (task or "")[:2000], (harness or "")[:100], (model or "")[:100],
         snapshot, json.dumps(refs), utc_now(), agent, key_id or ""))
    db.commit()
    how = ("Read the index. For every item whose when_to_use matches your task, call "
           "get_skill(refs=[...], session_id=...) with its ref before acting; batch several refs "
           "in one call. Refs are pinned to this snapshot, so the content will not change during "
           "your task. Follow the guardrails above at all times.")
    if not rows:
        how = "The catalog is empty for this account; proceed with your own judgment."
    if mode == "optimize":
        how += (" This is an agent-building run: when the task is finished, call "
                "log_episode(session_id, outcome, summary, corrections) describing what happened, "
                "including any corrections the user made.")
    return {
        "session_id": session_id, "snapshot": snapshot, "mode": mode,
        "agent": ({"ref": _ref(role), "title": role["title"], "body": role["body"][:2000]} if role else None),
        "guardrails": [{"ref": _ref(r), "title": r["title"],
                        "body": r["body"][:GUARDRAIL_INLINE_CHARS]} for r in guard_rows],
        "index": [{"ref": _ref(r), "kind": r["kind"], "title": r["title"],
                   "when_to_use": r["when_to_use"]} for r in rest],
        "omitted": omitted, "how_to_use": how,
    }


def get_skills(backend: Backend, user: str, refs: list[str], session_id: str = "",
               agent: str = "", restrict: bool = False) -> dict:
    """restrict=True: only items visible to `agent` resolve, for deployments that bind
    each caller to one profile, so an agent can't fetch another profile's skill by
    guessing its ref."""
    if not refs:
        raise ValueError("refs must name at least one catalog item")
    if len(refs) > MAX_REFS_PER_FETCH:
        raise ValueError(f"at most {MAX_REFS_PER_FETCH} refs per call")
    db = _db(backend, user)
    if session_id and not db.run(
            "SELECT 1 AS ok FROM catalog_sessions WHERE id = ? AND user_id = ?", (session_id, user)):
        raise ValueError(f"unknown session {session_id}")
    profiles = _profiles(_promoted(db, user)) if restrict else {}
    items, missing = [], []
    for raw in dict.fromkeys(refs):
        try:
            slug, version = parse_ref(raw)
        except ValueError:
            missing.append(raw)
            continue
        if version is None:
            rows = db.run("SELECT * FROM catalog_items WHERE user_id = ? AND slug = ? "
                          "AND status = 'promoted'", (user, slug))
        else:
            rows = db.run("SELECT * FROM catalog_items WHERE user_id = ? AND slug = ? AND version = ? "
                          "AND status IN ('promoted', 'retired')", (user, slug, version))
        if not rows or (restrict and (rows[0]["kind"] == "agent" or not _visible(slug, agent, profiles))):
            missing.append(raw)
            continue
        r = rows[0]
        items.append({"ref": _ref(r), "kind": r["kind"], "title": r["title"],
                      "when_to_use": r["when_to_use"], "body": r["body"]})
        db.run("INSERT INTO catalog_usage (user_id, session_id, slug, version, fetched_at) "
               "VALUES (?, ?, ?, ?, ?)", (user, session_id or "", r["slug"], r["version"], utc_now()))
    db.commit()
    return {"items": items, "missing": missing}


def _purge_expired(db: _Db) -> int:
    rows = db.run("SELECT COUNT(*) AS n FROM catalog_episodes WHERE expires_at < ?", (utc_now(),))
    db.run("DELETE FROM catalog_episodes WHERE expires_at < ?", (utc_now(),))
    return rows[0]["n"] if rows else 0


def log_episode(backend: Backend, user: str, session_id: str, outcome: str, summary: str,
                corrections: str = "", score: float | None = None) -> dict:
    if outcome not in config.EPISODE_OUTCOMES:
        raise ValueError(f"outcome must be one of: {', '.join(sorted(config.EPISODE_OUTCOMES))}")
    if score is not None and not 0.0 <= float(score) <= 1.0:
        raise ValueError("score must be between 0 and 1")
    if not (summary or "").strip():
        raise ValueError("summary must be non-empty")
    for name, text in (("summary", summary), ("corrections", corrections or "")):
        if len(text.encode("utf-8")) > MAX_EPISODE_TEXT:
            raise ValueError(f"{name} is over the {MAX_EPISODE_TEXT}-byte limit")
    db = _db(backend, user)
    rows = db.run("SELECT mode FROM catalog_sessions WHERE id = ? AND user_id = ?", (session_id, user))
    if not rows:
        raise ValueError(f"unknown session {session_id}")
    if rows[0]["mode"] != "optimize":
        raise ValueError("episodes are recorded only for agent-building runs: start the session "
                         "with agent_startup(mode='optimize')")
    used = sorted({f"{u['slug']}@v{u['version']}" for u in db.run(
        "SELECT slug, version FROM catalog_usage WHERE user_id = ? AND session_id = ?",
        (user, session_id))})
    purged = _purge_expired(db)
    now = datetime.now(timezone.utc)
    episode_id = f"epi_{ULID()}"
    db.run(
        "INSERT INTO catalog_episodes (id, user_id, session_id, outcome, score, summary, corrections, "
        "refs_used, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (episode_id, user, session_id, outcome, score, summary.strip(), (corrections or "").strip(),
         json.dumps(used), utc_now(),
         (now + timedelta(days=config.episode_ttl_days())).strftime("%Y-%m-%dT%H:%M:%SZ")))
    db.commit()
    return {"episode_id": episode_id, "credited": used, "expires_in_days": config.episode_ttl_days(),
            "purged_expired": purged}


# ---------------------------------------------------------------- rendering for agents
# Markdown, not JSON: the 2026-10-31 acceptance run showed a model reading a JSON
# blob of escaped bodies follows the guidance far less reliably than the same items
# delivered as plain Markdown documents.

def render_startup(s: dict) -> str:
    lines = [f"# Catalog session {s['session_id']}",
             f"snapshot {s['snapshot']} | mode {s['mode']}"
             + (f" | agent {s['agent']['ref']}" if s.get("agent") else ""), ""]
    if s.get("agent"):
        lines += [f"## Your role: {s['agent']['title']} (`{s['agent']['ref']}`)", "", s["agent"]["body"], ""]
    if s["guardrails"]:
        lines += ["## Guardrails - follow these at all times", ""]
        for g in s["guardrails"]:
            lines += [f"### {g['title']} (`{g['ref']}`)", "", g["body"], ""]
    if s["index"]:
        lines += [f"## Index - fetch what applies with get_skill(refs=[...], session_id=\"{s['session_id']}\")", ""]
        lines += [f"- `{i['ref']}` - **{i['title']}**: {i['when_to_use']}" for i in s["index"]]
        if s["omitted"]:
            lines.append(f"- ({s['omitted']} less relevant items omitted)")
        lines.append("")
    lines += ["## How to use", "", s["how_to_use"]]
    return "\n".join(lines)


def render_skills(res: dict) -> str:
    parts = []
    for i in res["items"]:
        parts.append(f"# {i['title']}\n`{i['ref']}` | when to use: {i['when_to_use']}\n\n{i['body']}")
    if res["missing"]:
        parts.append("Not found (drafts, unknown or retired-without-version refs): "
                     + ", ".join(f"`{m}`" for m in res["missing"]))
    return "\n\n---\n\n".join(parts) if parts else "No items found."


# ---------------------------------------------------------------- human views

def list_items(backend: Backend, user: str, status: str | None = None,
               include_body: bool = False) -> list[dict]:
    db = _db(backend, user)
    sql = ("SELECT slug, version, kind, title, when_to_use, status, created_by, created_at"
           + (", body" if include_body else "") + " FROM catalog_items WHERE user_id = ?")
    params: tuple = (user,)
    if status:
        sql += " AND status = ?"
        params += (status,)
    rows = db.run(sql.replace(", created_at", ", created_at, skills", 1) + " ORDER BY slug, version", params)
    for r in rows:
        r["ref"] = _ref(r)
        r["skills"] = json.loads(r.get("skills") or "[]")
    return rows


def show(backend: Backend, user: str, ref: str) -> dict:
    slug, version = parse_ref(ref)
    db = _db(backend, user)
    sql, params = "SELECT * FROM catalog_items WHERE user_id = ? AND slug = ?", (user, slug)
    if version is not None:
        sql, params = sql + " AND version = ?", params + (version,)
    rows = db.run(sql + " ORDER BY version DESC", params)
    if not rows:
        raise ValueError(f"no catalog item matching {ref}")
    r = rows[0]
    r["ref"] = _ref(r)
    r["history"] = _read_state(user, slug).get("history", [])
    return r


def stats(backend: Backend, user: str) -> list[dict]:
    db = _db(backend, user)
    out = {}
    for r in db.run("SELECT slug, version, kind, status FROM catalog_items WHERE user_id = ?", (user,)):
        out[f"{r['slug']}@v{r['version']}"] = {
            "ref": _ref(r), "status": r["status"], "fetches": 0, "sessions": 0,
            "episodes": 0, "success": 0, "failure": 0, "partial": 0}
    for u in db.run("SELECT slug, version, COUNT(*) AS n, COUNT(DISTINCT session_id) AS s "
                    "FROM catalog_usage WHERE user_id = ? GROUP BY slug, version", (user,)):
        key = f"{u['slug']}@v{u['version']}"
        if key in out:
            out[key]["fetches"], out[key]["sessions"] = u["n"], u["s"]
    # who actually used each version: the agent profile when one was in play, else harness
    who = {r["id"]: (r["agent"] or r["harness"] or "unknown") for r in db.run(
        "SELECT id, agent, harness FROM catalog_sessions WHERE user_id = ?", (user,))}
    for u in db.run("SELECT DISTINCT slug, version, session_id FROM catalog_usage WHERE user_id = ?", (user,)):
        key = f"{u['slug']}@v{u['version']}"
        if key in out:
            label = who.get(u["session_id"], "unknown")
            used = out[key].setdefault("used_by", {})
            used[label] = used.get(label, 0) + 1
    for e in db.run("SELECT outcome, refs_used FROM catalog_episodes WHERE user_id = ? "
                    "AND expires_at >= ?", (user, utc_now())):
        for key in json.loads(e["refs_used"]):
            if key in out:
                out[key]["episodes"] += 1
                if e["outcome"] in out[key]:
                    out[key][e["outcome"]] += 1
    return sorted(out.values(), key=lambda x: x["ref"])


def episodes(backend: Backend, user: str, limit: int = 20) -> list[dict]:
    db = _db(backend, user)
    rows = db.run("SELECT id, session_id, outcome, score, summary, corrections, refs_used, created_at "
                  "FROM catalog_episodes WHERE user_id = ? AND expires_at >= ? "
                  "ORDER BY created_at DESC LIMIT ?", (user, utc_now(), max(1, min(int(limit), 200))))
    for r in rows:
        r["refs_used"] = json.loads(r["refs_used"])
    return rows
