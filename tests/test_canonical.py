from openbrainstore.canonical import Memory, from_markdown, new_memory_id, to_markdown


def test_roundtrip():
    mem = Memory(
        id=new_memory_id(),
        user="testuser",
        type="decision",
        created="2026-07-18T14:02:11Z",
        updated="2026-07-18T14:02:11Z",
        source_harness="cli",
        entities=["project-alpha", "sarah"],
        tags=["architecture", "postgres"],
        kv={"project": "okf", "priority": "high", "count": 3, "done": True},
        links=["mem_01OTHER"],
        body="Decided to use warm standby replication instead of Patroni for MVP.",
    )
    text = to_markdown(mem)
    assert text.startswith("---\n")
    back = from_markdown(text)
    assert back == mem


def test_ids_are_unique_and_sortable():
    a, b = new_memory_id(), new_memory_id()
    assert a != b
    assert a.startswith("mem_")
