"""OKF export profile (docs/specs/okf-export-profile.md). Bundle construction
is pure (Memory objects in, dict out), so most of this needs no DB; one
end-to-end test drives service.export through a backend."""

import tarfile

import yaml

from openbrainstore import okf_export, service
from openbrainstore.canonical import Memory, new_memory_id, utc_now


def _mem(body, **over):
    now = utc_now()
    base = dict(
        id=new_memory_id(), user="gh_12345678", type="fact",
        created=now, updated=now, body=body,
        source_harness="claude-code", entities=[], tags=[], kv={}, links=[],
    )
    base.update(over)
    return Memory(**base)


def _split_front(text):
    assert text.startswith("---\n")
    _, fm, _ = text.split("---\n", 2)
    return yaml.safe_load(fm)


def test_memory_file_frontmatter_is_portable_and_identity_stripped():
    m = _mem("A decision body.", type="decision",
             entities=["project-alpha"], tags=["arch"],
             kv={"project": "okf"}, source_harness="codex")
    bundle = okf_export.build_bundle([m])
    text = bundle[f"memories/{m.id}.md"]
    fm = _split_front(text)
    assert set(fm) == {"id", "type", "created", "timestamp", "entities", "tags", "obs"}
    assert fm["timestamp"] == m.updated  # mapped from updated
    assert "user" not in fm and "gh_12345678" not in text  # no tenant identity
    assert fm["obs"]["source_harness"] == "codex"
    assert fm["obs"]["kv"] == {"project": "okf"}


def test_include_identity_option_puts_user_under_obs():
    m = _mem("x", user="gh_999")
    text = okf_export.build_bundle([m], include_identity=True)[f"memories/{m.id}.md"]
    assert _split_front(text)["obs"]["user"] == "gh_999"


def test_links_become_markdown_links_and_raw_ids_survive():
    a = _mem("The base decision.", type="decision")
    b = _mem("A follow-up event.", type="event", links=[a.id])
    bundle = okf_export.build_bundle([a, b])
    bfile = bundle[f"memories/{b.id}.md"]
    assert "## Related" in bfile
    assert f"[The base decision.]({a.id}.md)" in bfile   # relative sibling link
    assert _split_front(bfile)["obs"]["links"] == [a.id]  # lossless raw ids


def test_index_groups_by_type_and_links_entities():
    a = _mem("Alpha fact.", type="fact", entities=["proj"])
    b = _mem("A decision.", type="decision", entities=["proj", "sarah"])
    bundle = okf_export.build_bundle([a, b])
    idx = bundle["index.md"]
    assert "### fact (1)" in idx and "### decision (1)" in idx
    assert f"(memories/{a.id}.md)" in idx
    assert "(entities/proj.md)" in idx and "(entities/sarah.md)" in idx
    # entity page links back to the memories that mention it
    proj = bundle["entities/proj.md"]
    assert f"(../memories/{a.id}.md)" in proj and f"(../memories/{b.id}.md)" in proj
    assert "entities/sarah.md" in "".join(bundle)  # sarah page exists
    assert bundle["entities/sarah.md"].count("../memories/") == 1


def test_readme_present_and_no_entities_section_when_none():
    m = _mem("no entities here")
    bundle = okf_export.build_bundle([m])
    assert "Open Knowledge Format" in bundle["README.md"]
    assert "By entity" not in bundle["index.md"]
    assert not any(k.startswith("entities/") for k in bundle)


def test_end_to_end_export_through_backend(backend):
    a = service.remember(backend, "Base decision about storage.", "decision",
                         entities=["okf"], tags=["arch"], user="testuser")
    service.remember(backend, "Follow-up event.", "event",
                     links=[a["id"]], entities=["okf"], user="testuser")
    res = service.export(user="testuser", profile="okf")
    assert res["profile"] == "okf" and res["memories"] == 2
    with tarfile.open(res["path"]) as tar:
        names = tar.getnames()
        root = names[0].split("/")[0]
        assert f"{root}/README.md" in names
        assert f"{root}/index.md" in names
        assert f"{root}/memories/{a['id']}.md" in names
        assert f"{root}/entities/okf.md" in names
        body = tar.extractfile(f"{root}/memories/{a['id']}.md").read().decode()
    assert "testuser" not in body  # identity stripped end to end


def test_raw_profile_unchanged(backend):
    service.remember(backend, "raw fidelity check", "fact", user="testuser")
    res = service.export(user="testuser", profile="raw")
    assert res["profile"] == "raw"
    with tarfile.open(res["path"]) as tar:
        # raw keeps the internal version-stamped layout: memories/<id>/<version>.md
        assert any(n.count("/") == 2 and n.endswith(".md") for n in tar.getnames())
