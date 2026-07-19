"""Contact / commercial-license inquiries from the public website. Same shape
as web_signup.py (pure validate() + Postgres-backed submit(), honeypot spam
defense) but a separate table so sales leads stay out of the beta waitlist."""

import re

from . import config

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PURPOSES = {"commercial", "general", "partnership", "other"}
_TRUTHY = {"true", "on", "1", "yes"}

TABLE = """
CREATE TABLE IF NOT EXISTS contact_requests (
  id         BIGSERIAL PRIMARY KEY,
  name       TEXT NOT NULL,
  email      TEXT NOT NULL,
  company    TEXT,
  purpose    TEXT NOT NULL,
  message    TEXT NOT NULL,
  ip         TEXT,
  status     TEXT NOT NULL DEFAULT 'new',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class ContactError(ValueError):
    """Message is safe to show the user."""


def validate(data: dict) -> dict | None:
    """Cleaned record, or None if the honeypot tripped (report success, store
    nothing). Raises ContactError with a user-facing message otherwise."""
    if (data.get("company_website") or "").strip():
        return None  # honeypot

    name = (data.get("name") or "").strip()[:200]
    email = (data.get("email") or "").strip()
    company = (data.get("company") or "").strip()[:200]
    purpose = (data.get("purpose") or "").strip().lower()
    message = (data.get("message") or "").strip()[:5000]

    if not name:
        raise ContactError("Please add your name.")
    if not _EMAIL_RE.match(email):
        raise ContactError("Enter a valid email address.")
    if purpose not in PURPOSES:
        raise ContactError("Pick what this is about.")
    if not message:
        raise ContactError("Add a short message so we know how to help.")

    return {"name": name, "email": email, "company": company,
            "purpose": purpose, "message": message}


_initialized = False


def _connect():
    from psycopg.rows import dict_row

    from .backends.postgres_backend import _connect_with_wsl_wake

    conn = _connect_with_wsl_wake(config.pg_dsn())
    conn.row_factory = dict_row
    return conn


def submit(data: dict, ip: str | None = None) -> None:
    """Validate and persist a contact request. Blocking — call off the loop."""
    global _initialized
    cleaned = validate(data)
    if cleaned is None:
        return  # honeypot

    conn = _connect()
    try:
        if not _initialized:
            with conn.cursor() as cur:
                cur.execute(TABLE)
            conn.commit()
            _initialized = True
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO contact_requests (name, email, company, purpose, message, ip)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (cleaned["name"], cleaned["email"], cleaned["company"],
                 cleaned["purpose"], cleaned["message"], ip),
            )
        conn.commit()
    finally:
        conn.close()
