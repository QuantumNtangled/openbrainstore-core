from openbrainstore.normalize import (
    coerce_value,
    normalize_entities,
    normalize_key,
    normalize_kv,
)


def test_key_normalization():
    assert normalize_key("Project Name") == "project_name"
    assert normalize_key("due-date") == "due_date"
    assert normalize_key("  Weird!!Key  ") == "weirdkey"
    assert normalize_key("a__b---c") == "a_b_c"


def test_value_coercion():
    assert coerce_value("true") is True
    assert coerce_value("False") is False
    assert coerce_value("42") == 42
    assert coerce_value("-3.14") == -3.14
    assert coerce_value("2026-07-18") == "2026-07-18"
    assert coerce_value("07/18/2026") == "2026-07-18"
    assert coerce_value("just text") == "just text"
    assert coerce_value(7) == 7


def test_kv_and_entities():
    assert normalize_kv({"Due Date": "01/15/2026"}) == {"due_date": "2026-01-15"}
    assert normalize_entities(["Project Alpha", "SARAH", "project alpha"]) == [
        "project-alpha",
        "sarah",
    ]
