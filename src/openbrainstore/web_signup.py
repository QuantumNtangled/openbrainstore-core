"""Beta-access signups from the public website. Standalone (not a memory/
Backend concern): a small Postgres table the app writes to from a public
POST route. Validation is a pure function so it's testable without a DB.

Spam defense is proportionate to a beta: a honeypot field plus the transport
rate limiter (per-IP unauthenticated cap). No CAPTCHA, no third-party JS."""

import re

from . import config

_GH_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")  # GitHub username rules
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLES = {"indie", "engineer", "founder", "researcher", "student", "other"}
_TRUTHY = {"true", "on", "1", "yes"}

TABLE = """
CREATE TABLE IF NOT EXISTS beta_signups (
  id              BIGSERIAL PRIMARY KEY,
  github_username TEXT NOT NULL,
  email           TEXT NOT NULL,
  role            TEXT NOT NULL,
  use_case        TEXT,
  ip              TEXT,
  status          TEXT NOT NULL DEFAULT 'pending',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_beta_signups_gh
  ON beta_signups (lower(github_username));
"""


class SignupError(ValueError):
    """Message is safe to show the user."""


def _is_true(v) -> bool:
    return v is True or str(v).strip().lower() in _TRUTHY


def validate(data: dict) -> dict | None:
    """Return a cleaned record, or None when the honeypot is tripped (caller
    should report success but store nothing, so bots learn nothing). Raises
    SignupError with a user-facing message on real validation failures."""
    if (data.get("company_website") or "").strip():
        return None  # honeypot: a real user never fills this hidden field

    gh = (data.get("github_username") or "").strip().lstrip("@")
    email = (data.get("email") or "").strip()
    role = (data.get("role") or "").strip().lower()
    use_case = (data.get("use_case") or "").strip()[:1000]

    if not _GH_RE.match(gh):
        raise SignupError("Enter a valid GitHub username (letters, numbers, hyphens).")
    if not _EMAIL_RE.match(email):
        raise SignupError("Enter a valid email address.")
    if role not in ROLES:
        raise SignupError("Pick the option that best describes you.")
    if not _is_true(data.get("acknowledged")):
        raise SignupError("Please check the box acknowledging what beta means.")

    return {"github_username": gh, "email": email, "role": role, "use_case": use_case}


_initialized = False


def _connect():
    from .backends.postgres_backend import _connect_with_wsl_wake
    from psycopg.rows import dict_row

    conn = _connect_with_wsl_wake(config.pg_dsn())
    conn.row_factory = dict_row
    return conn


def submit(data: dict, ip: str | None = None) -> None:
    """Validate and persist a signup. Idempotent per GitHub username (a repeat
    submission updates the existing row). Blocking — call off the event loop."""
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
                """INSERT INTO beta_signups (github_username, email, role, use_case, ip)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (lower(github_username)) DO UPDATE SET
                     email = EXCLUDED.email, role = EXCLUDED.role,
                     use_case = EXCLUDED.use_case, ip = EXCLUDED.ip,
                     updated_at = now()""",
                (cleaned["github_username"], cleaned["email"], cleaned["role"],
                 cleaned["use_case"], ip),
            )
        conn.commit()
    finally:
        conn.close()
