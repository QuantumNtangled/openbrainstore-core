"""Deterministic normalization for kv keys/values and entity names.
No LLM in the write path — everything here must be pure and predictable."""

import re
from datetime import datetime


def normalize_key(key: str) -> str:
    k = str(key).strip().lower()
    k = re.sub(r"[\s\-]+", "_", k)
    k = re.sub(r"[^a-z0-9_]", "", k)
    k = re.sub(r"_+", "_", k).strip("_")
    return k or "key"


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d")


def coerce_value(value):
    """Coerce string booleans, numbers, and dates to canonical forms.
    Anything unrecognized passes through unchanged."""
    if isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        return value
    s = value.strip()
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s  # already a canonical date
    # ISO datetime -> normalized ISO string
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def normalize_kv(kv: dict | None) -> dict:
    if not kv:
        return {}
    out = {}
    for k, v in kv.items():
        out[normalize_key(k)] = coerce_value(v)
    return out


def normalize_entity(name: str) -> str:
    e = str(name).strip().lower()
    e = re.sub(r"[\s_]+", "-", e)
    e = re.sub(r"[^a-z0-9\-]", "", e)
    e = re.sub(r"-+", "-", e).strip("-")
    return e


def normalize_entities(entities: list | None) -> list[str]:
    if not entities:
        return []
    seen: list[str] = []
    for e in entities:
        n = normalize_entity(e)
        if n and n not in seen:
            seen.append(n)
    return seen


def normalize_tags(tags: list | None) -> list[str]:
    # same shape rules as entities
    return normalize_entities(tags)
