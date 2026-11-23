"""Skill catalog (docs/specs/skill-catalog.md): versioned items served by reference,
human-only promotion, pinned refs, usage + episode attribution, tenant isolation."""

import asyncio
import json
import os
import subprocess
import sys

import pytest

from openbrainstore import catalog, config

U, OTHER = "testuser", "otheruser"


@pytest.fixture()
def cat(backend):
    for user in (U, OTHER):
        catalog.clear_user(backend, user)
    yield backend
    for user in (U, OTHER):
        catalog.clear_user(backend, user)


def _add(b, slug, when="when the task involves widgets", body="Do the widget thing.", **kw):
    kw.setdefault("promote", True)
    return catalog.add_version(b, U, slug, kw.pop("title", slug.title()), when, body, **kw)


def test_import_dir_and_startup(cat, tmp_path):
    d = tmp_path / "CATALOG"
    d.mkdir()
    (d / "INDEX.md").write_text(
        "# Catalog\n\n"
        "- coding-workflow: Start here on every task -> CATALOG/coding-workflow.md\n"
        "- final-checklist: Run before answering -> CATALOG/final-checklist.md\n"
        "- no-guessing: Always applies -> CATALOG/no-guessing.md\n", encoding="utf-8")
    (d / "coding-workflow.md").write_text("# Coding workflow\n\n1. Read.\n2. Code.\n", encoding="utf-8")
    (d / "final-checklist.md").write_text("# Final checklist\n\n- [ ] verify\n", encoding="utf-8")
    (d / "no-guessing.md").write_text("---\nkind: guardrail\ntitle: Never guess\n---\nQuery instead of guessing.",
                                      encoding="utf-8")
    res = catalog.import_dir(cat, U, str(d))
    assert res["imported"] == 3
    s = catalog.startup(cat, U, task="code an encounter")
    refs = {i["ref"] for i in s["index"]}
    assert refs == {"procedure:coding-workflow@v1", "checklist:final-checklist@v1"}
    assert s["guardrails"] == [{"ref": "guardrail:no-guessing@v1", "title": "Never guess",
                                "body": "Query instead of guessing."}]
    assert s["session_id"].startswith("ses_") and s["snapshot"].startswith("cat_")
    assert catalog.startup(cat, U)["snapshot"] == s["snapshot"]  # same refs -> same snapshot
    body = catalog.get_skills(cat, U, ["procedure:coding-workflow@v1"])["items"][0]["body"]
    assert body.startswith("1. Read.")  # heading became the title, not part of the body


def test_drafts_are_never_served(cat):
    out = catalog.propose(cat, U, "new-trick", "New trick", "when tricks are needed", "Trick body.")
    assert out == {"ref": "skill:new-trick@v1", "status": "draft",
                   "note": "Saved as a draft. Drafts are never served; a human must review and promote it."}
    assert catalog.startup(cat, U)["index"] == []
    got = catalog.get_skills(cat, U, ["new-trick", "skill:new-trick@v1"])
    assert got["items"] == [] and set(got["missing"]) == {"new-trick", "skill:new-trick@v1"}
    catalog.promote_version(cat, U, "new-trick")
    assert [i["ref"] for i in catalog.startup(cat, U)["index"]] == ["skill:new-trick@v1"]


def test_pinned_ref_survives_new_promotion(cat):
    _add(cat, "widgets", body="Version one.")
    s1 = catalog.startup(cat, U)
    catalog.add_version(cat, U, "widgets", "Widgets", "when the task involves widgets", "Version two.",
                        promote=True)
    s2 = catalog.startup(cat, U)
    assert [i["ref"] for i in s2["index"]] == ["skill:widgets@v2"]
    assert s1["snapshot"] != s2["snapshot"]
    pinned = catalog.get_skills(cat, U, ["skill:widgets@v1"])["items"]
    assert pinned[0]["body"] == "Version one."  # old session keeps its content
    assert catalog.get_skills(cat, U, ["widgets"])["items"][0]["body"] == "Version two."
    statuses = {r["version"]: r["status"] for r in catalog.list_items(cat, U)}
    assert statuses == {1: "retired", 2: "promoted"}


def test_retire_removes_from_index_but_pins_resolve(cat):
    _add(cat, "widgets")
    catalog.retire(cat, U, "widgets")
    assert catalog.startup(cat, U)["index"] == []
    assert catalog.get_skills(cat, U, ["widgets"])["missing"] == ["widgets"]
    assert catalog.get_skills(cat, U, ["skill:widgets@v1"])["items"]
    with pytest.raises(ValueError, match="no promoted version"):
        catalog.retire(cat, U, "widgets")


def test_usage_and_episode_crediting(cat):
    _add(cat, "alpha")
    _add(cat, "beta")
    _add(cat, "gamma")
    s = catalog.startup(cat, U, mode="optimize")
    assert "log_episode" in s["how_to_use"]
    catalog.get_skills(cat, U, ["skill:alpha@v1", "skill:beta@v1"], session_id=s["session_id"])
    ep = catalog.log_episode(cat, U, s["session_id"], "success", "Coded the case; grader passed.",
                             corrections="none", score=1.0)
    assert ep["credited"] == ["alpha@v1", "beta@v1"] and ep["episode_id"].startswith("epi_")
    by_ref = {r["ref"]: r for r in catalog.stats(cat, U)}
    assert by_ref["skill:alpha@v1"]["fetches"] == 1 and by_ref["skill:alpha@v1"]["success"] == 1
    assert by_ref["skill:gamma@v1"]["episodes"] == 0  # never fetched, never credited
    assert catalog.episodes(cat, U)[0]["refs_used"] == ["alpha@v1", "beta@v1"]


def test_log_episode_guards(cat):
    ground = catalog.startup(cat, U)
    with pytest.raises(ValueError, match="agent-building runs"):
        catalog.log_episode(cat, U, ground["session_id"], "success", "x")
    opt = catalog.startup(cat, U, mode="optimize")
    for kwargs, msg in (({"outcome": "great"}, "outcome must be"),
                        ({"score": 1.5}, "between 0 and 1"),
                        ({"summary": "  "}, "non-empty"),
                        ({"summary": "x" * 9000}, "byte limit")):
        args = {"outcome": "success", "summary": "ok", **kwargs}
        with pytest.raises(ValueError, match=msg):
            catalog.log_episode(cat, U, opt["session_id"], **args)
    with pytest.raises(ValueError, match="unknown session"):
        catalog.log_episode(cat, U, "ses_nope", "success", "x")
    with pytest.raises(ValueError, match="mode must be"):
        catalog.startup(cat, U, mode="turbo")


def test_expired_episodes_are_purged(cat, monkeypatch):
    opt = catalog.startup(cat, U, mode="optimize")
    monkeypatch.setenv("OBS_EPISODE_TTL_DAYS", "-1")  # born already expired
    catalog.log_episode(cat, U, opt["session_id"], "failure", "first")
    monkeypatch.setenv("OBS_EPISODE_TTL_DAYS", "30")
    out = catalog.log_episode(cat, U, opt["session_id"], "success", "second")
    assert out["purged_expired"] == 1
    assert [e["summary"] for e in catalog.episodes(cat, U)] == ["second"]


def test_validation(cat):
    for kwargs, msg in (({"slug": "Bad Slug"}, "slug must match"),
                        ({"kind": "spell"}, "kind must be"),
                        ({"when": "word " * 41}, "when_to_use must be"),
                        ({"body": "  "}, "body must be"),
                        ({"body": "x" * (17 * 1024)}, "body must be")):
        args = {"slug": "ok-slug", "kind": "skill", "when": "when needed", "body": "Body.", **kwargs}
        with pytest.raises(ValueError, match=msg):
            catalog.add_version(cat, U, args["slug"], "Title", args["when"], args["body"], kind=args["kind"])
    with pytest.raises(ValueError, match="at most 10"):
        catalog.get_skills(cat, U, [f"x{i}" for i in range(11)])
    assert catalog.get_skills(cat, U, ["not a ref!"])["missing"] == ["not a ref!"]


def test_tenant_isolation(cat):
    _add(cat, "secret-sauce", body="Only for testuser.")
    s = catalog.startup(cat, U, mode="optimize")
    assert catalog.startup(cat, OTHER)["index"] == []
    assert catalog.get_skills(cat, OTHER, ["secret-sauce", "skill:secret-sauce@v1"])["items"] == []
    with pytest.raises(ValueError, match="unknown session"):
        catalog.log_episode(cat, OTHER, s["session_id"], "success", "hijack")
    with pytest.raises(ValueError, match="unknown session"):
        catalog.get_skills(cat, OTHER, ["secret-sauce"], session_id=s["session_id"])


def test_reindex_rebuilds_catalog_from_blobs(cat, monkeypatch):
    _add(cat, "alpha")
    catalog.add_version(cat, U, "alpha", "Alpha", "when alpha", "Draft two.")
    db = catalog._db(cat, U)
    db.run("DELETE FROM catalog_items WHERE user_id = ?", (U,))
    db.commit()
    assert catalog.list_items(cat, U) == []
    monkeypatch.setenv("OBS_CATALOG", "1")
    from openbrainstore import service
    assert service.reindex(cat)["catalog_items"] == 1
    assert {r["version"]: r["status"] for r in catalog.list_items(cat, U)} == {1: "promoted", 2: "draft"}


def test_index_cap_ranks_by_task(cat, monkeypatch):
    monkeypatch.setenv("OBS_CATALOG_INDEX_CAP", "2")
    _add(cat, "sepsis-rules", when="sepsis septic shock organ dysfunction")
    _add(cat, "fracture-rules", when="fracture osteoporosis fall")
    _add(cat, "covid-rules", when="covid bronchitis pneumonia")
    _add(cat, "diabetes-rules", when="diabetes kidney hypertension")
    s = catalog.startup(cat, U, task="patient with septic shock and diabetes with kidney disease")
    assert {i["ref"] for i in s["index"]} == {"skill:sepsis-rules@v1", "skill:diabetes-rules@v1"}
    assert s["omitted"] == 2


def _tool_names(extra_env: dict) -> set:
    env = {k: v for k, v in os.environ.items() if k != "OBS_CATALOG"}
    env.update(extra_env)
    code = ("import asyncio, json, openbrainstore.server as s; "
            "print(json.dumps(sorted(t.name for t in asyncio.run(s.mcp.list_tools()))))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_tools_registered_only_with_flag(tmp_path, monkeypatch):
    base = {"OBS_DATA_DIR": str(tmp_path), "OBS_BACKEND": "sqlite"}
    off = _tool_names(base)
    on = _tool_names({**base, "OBS_CATALOG": "1"})
    catalog_tools = {"agent_startup", "get_skill", "log_episode", "propose_skill"}
    assert not (off & catalog_tools)
    assert on - off == catalog_tools


def test_agent_facing_markdown_rendering(cat):
    _add(cat, "no-guessing", kind="guardrail", body="Query instead of guessing.", title="Never guess")
    _add(cat, "widgets", body="Line one.\nLine two.")
    s = catalog.startup(cat, U)
    md = catalog.render_startup(s)
    assert md.startswith(f"# Catalog session {s['session_id']}")
    assert "## Guardrails" in md and "Query instead of guessing." in md
    assert "- `skill:widgets@v1` - **Widgets**: when the task involves widgets" in md
    out = catalog.render_skills(catalog.get_skills(cat, U, ["widgets", "nope"]))
    assert "# Widgets\n`skill:widgets@v1`" in out and "Line one.\nLine two." in out  # real newlines
    assert "Not found" in out and "`nope`" in out


def test_account_switch_default_rule(cat, monkeypatch):
    monkeypatch.setenv("OBS_CATALOG", "1")
    assert config.catalog_mode() == "optin"
    assert catalog.account_enabled(cat, U) is False          # new account starts off
    _add(cat, "widgets")
    assert catalog.account_enabled(cat, U) is True           # existing catalog keeps working
    catalog.set_account_enabled(cat, U, False)
    assert catalog.account_enabled(cat, U) is False          # explicit choice wins
    catalog.set_account_enabled(cat, U, True)
    assert catalog.account_enabled(cat, U) is True
    assert catalog.account_enabled(cat, OTHER) is False      # per account, not global
    monkeypatch.setenv("OBS_CATALOG", "all")
    assert catalog.account_enabled(cat, OTHER) is True
    monkeypatch.setenv("OBS_CATALOG", "")
    assert catalog.account_enabled(cat, U) is False


def test_reject_and_restore(cat):
    catalog.propose(cat, U, "risky", "Risky", "when tempted", "Do the risky thing.")
    assert catalog.reject(cat, U, "risky") == {"slug": "risky", "rejected": "v1"}
    assert catalog.get_skills(cat, U, ["risky", "skill:risky@v1"])["items"] == []
    with pytest.raises(ValueError, match="no draft to reject"):
        catalog.reject(cat, U, "risky")
    assert catalog.promote_version(cat, U, "risky", 1)["status"] == "promoted"  # undo
    assert {r["status"] for r in catalog.list_items(cat, U)} == {"promoted"}


def _profiles_fixture(b):
    for slug in ("review-checklist", "shared-rule", "deploy-steps"):
        _add(b, slug, body=f"Body of {slug}.")
    catalog.add_version(b, U, "reviewer", "Reviewer", "code review agent", "You review code.",
                        kind="agent", skills=["review-checklist"], promote=True)
    catalog.add_version(b, U, "deployer", "Deployer", "deploy agent", "You deploy.",
                        kind="agent", skills=["deploy-steps"], promote=True)


def test_profile_visibility(cat):
    _profiles_fixture(cat)
    mine = catalog.startup(cat, U)
    assert {i["ref"] for i in mine["index"]} == {"skill:shared-rule@v1"}       # assigned skills hidden
    assert mine["agent"] is None
    rev = catalog.startup(cat, U, agent="reviewer")
    assert {i["ref"] for i in rev["index"]} == {"skill:review-checklist@v1", "skill:shared-rule@v1"}
    assert rev["agent"]["ref"] == "agent:reviewer@v1" and rev["agent"]["body"] == "You review code."
    assert "## Your role: Reviewer" in catalog.render_startup(rev)
    with pytest.raises(ValueError, match="no live agent profile"):
        catalog.startup(cat, U, agent="ghost")
    row = catalog._db(cat, U).run("SELECT agent, key_id FROM catalog_sessions WHERE id = ?", (rev["session_id"],))[0]
    assert row == {"agent": "reviewer", "key_id": ""}


def test_restricted_fetch_and_used_by(cat):
    _profiles_fixture(cat)
    s = catalog.startup(cat, U, agent="reviewer", harness="langgraph")
    got = catalog.get_skills(cat, U, ["review-checklist", "deploy-steps", "shared-rule", "agent:deployer@v1"],
                             session_id=s["session_id"], agent="reviewer", restrict=True)
    assert {i["ref"] for i in got["items"]} == {"skill:review-checklist@v1", "skill:shared-rule@v1"}
    assert set(got["missing"]) == {"deploy-steps", "agent:deployer@v1"}
    assert catalog.get_skills(cat, U, ["deploy-steps"])["items"]                # the owner can fetch anything
    by_ref = {r["ref"]: r for r in catalog.stats(cat, U)}
    assert by_ref["skill:review-checklist@v1"]["used_by"] == {"reviewer": 1}


def test_profile_skill_validation(cat):
    with pytest.raises(ValueError, match="only agent profiles"):
        catalog.add_version(cat, U, "plain", "Plain", "when plain", "Body.", skills=["x-y"])
    with pytest.raises(ValueError, match="slugs"):
        catalog.add_version(cat, U, "loop", "Loop", "when looping", "Body.", kind="agent", skills=["loop"])
    out = catalog.add_version(cat, U, "solo", "Solo", "lone agent", "You work alone.", kind="agent",
                              skills=["a-skill"], promote=True)
    assert out["ref"] == "agent:solo@v1"
    listed = [i for i in catalog.list_items(cat, U) if i["slug"] == "solo"][0]
    assert listed["skills"] == ["a-skill"]


def test_migration_adds_new_columns(tmp_path, monkeypatch):
    """Production created the catalog tables before profiles existed; the new columns
    must be added in place."""
    import sqlite3
    monkeypatch.setenv("OBS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OBS_BACKEND", "sqlite")
    conn = sqlite3.connect(tmp_path / "projections.db")
    conn.executescript("""
      CREATE TABLE catalog_items (user_id TEXT NOT NULL, slug TEXT NOT NULL, version INTEGER NOT NULL,
        kind TEXT NOT NULL, title TEXT NOT NULL, when_to_use TEXT NOT NULL, body TEXT NOT NULL,
        status TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, slug, version));
      CREATE TABLE catalog_sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mode TEXT NOT NULL,
        task TEXT NOT NULL, harness TEXT NOT NULL, model TEXT NOT NULL, snapshot TEXT NOT NULL,
        refs TEXT NOT NULL, created_at TEXT NOT NULL);
      INSERT INTO catalog_items VALUES ('u','old-item',1,'skill','Old','when old','Body.','promoted','x','2026-01-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()
    catalog._initialized.clear()
    from openbrainstore.backends import get_backend
    with get_backend() as b:
        assert [i["skills"] for i in catalog.list_items(b, "u")] == [[]]
        assert catalog.startup(b, "u")["index"][0]["ref"] == "skill:old-item@v1"
