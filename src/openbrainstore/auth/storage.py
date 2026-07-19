"""Persistence for OAuth state: registered clients, pending GitHub round-trips,
authorization codes, and access/refresh tokens.

Postgres in the cloud (survives restarts — nobody gets logged out by a deploy);
in-memory for tests and any non-postgres run. Values are stored as JSON dicts;
the provider owns (de)serialization to SDK models."""

import json
import time
from abc import ABC, abstractmethod

from .. import config


class AuthStorage(ABC):
    # clients
    @abstractmethod
    def put_client(self, client_id: str, data: dict) -> None: ...

    @abstractmethod
    def get_client(self, client_id: str) -> dict | None: ...

    # pending upstream (GitHub) round-trips, keyed by our state nonce
    @abstractmethod
    def put_state(self, state: str, data: dict, ttl_seconds: int) -> None: ...

    @abstractmethod
    def pop_state(self, state: str) -> dict | None: ...

    # authorization codes
    @abstractmethod
    def put_code(self, code: str, data: dict, expires_at: float) -> None: ...

    @abstractmethod
    def get_code(self, code: str) -> dict | None: ...

    @abstractmethod
    def delete_code(self, code: str) -> None: ...

    # tokens (kind: access | refresh)
    @abstractmethod
    def put_token(self, token: str, kind: str, data: dict, expires_at: float | None) -> None: ...

    @abstractmethod
    def get_token(self, token: str, kind: str) -> dict | None: ...

    @abstractmethod
    def delete_token(self, token: str) -> None: ...


class InMemoryAuthStorage(AuthStorage):
    """Test/dev storage. Expiry enforced on read."""

    def __init__(self) -> None:
        self._clients: dict[str, dict] = {}
        self._states: dict[str, tuple[dict, float]] = {}
        self._codes: dict[str, tuple[dict, float]] = {}
        self._tokens: dict[str, tuple[str, dict, float | None]] = {}

    def put_client(self, client_id, data):
        self._clients[client_id] = data

    def get_client(self, client_id):
        return self._clients.get(client_id)

    def put_state(self, state, data, ttl_seconds):
        self._states[state] = (data, time.time() + ttl_seconds)

    def pop_state(self, state):
        entry = self._states.pop(state, None)
        if entry is None or entry[1] < time.time():
            return None
        return entry[0]

    def put_code(self, code, data, expires_at):
        self._codes[code] = (data, expires_at)

    def get_code(self, code):
        entry = self._codes.get(code)
        if entry is None or entry[1] < time.time():
            return None
        return entry[0]

    def delete_code(self, code):
        self._codes.pop(code, None)

    def put_token(self, token, kind, data, expires_at):
        self._tokens[token] = (kind, data, expires_at)

    def get_token(self, token, kind):
        entry = self._tokens.get(token)
        if entry is None or entry[0] != kind:
            return None
        if entry[2] is not None and entry[2] < time.time():
            return None
        return entry[1]

    def delete_token(self, token):
        self._tokens.pop(token, None)


PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id  TEXT PRIMARY KEY,
  data       JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS oauth_states (
  state      TEXT PRIMARY KEY,
  data       JSONB NOT NULL,
  expires_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code       TEXT PRIMARY KEY,
  data       JSONB NOT NULL,
  expires_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token      TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  data       JSONB NOT NULL,
  expires_at DOUBLE PRECISION
);
"""


class PostgresAuthStorage(AuthStorage):
    def __init__(self) -> None:
        import psycopg
        from psycopg.rows import dict_row
        from ..backends.postgres_backend import _connect_with_wsl_wake

        self.conn = _connect_with_wsl_wake(config.pg_dsn())
        self.conn.row_factory = dict_row
        with self.conn.cursor() as cur:
            cur.execute(PG_SCHEMA)
        self.conn.commit()
        self._Jsonb = __import__("psycopg.types.json", fromlist=["Jsonb"]).Jsonb

    def _one(self, sql: str, params: tuple):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _exec(self, sql: str, params: tuple) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
        self.conn.commit()

    def put_client(self, client_id, data):
        self._exec(
            """INSERT INTO oauth_clients (client_id, data) VALUES (%s, %s)
               ON CONFLICT (client_id) DO UPDATE SET data = EXCLUDED.data""",
            (client_id, self._Jsonb(data)),
        )

    def get_client(self, client_id):
        row = self._one("SELECT data FROM oauth_clients WHERE client_id = %s", (client_id,))
        return row["data"] if row else None

    def put_state(self, state, data, ttl_seconds):
        self._exec(
            "INSERT INTO oauth_states (state, data, expires_at) VALUES (%s, %s, %s)",
            (state, self._Jsonb(data), time.time() + ttl_seconds),
        )

    def pop_state(self, state):
        row = self._one(
            "DELETE FROM oauth_states WHERE state = %s RETURNING data, expires_at", (state,)
        )
        self.conn.commit()
        if row is None or row["expires_at"] < time.time():
            return None
        return row["data"]

    def put_code(self, code, data, expires_at):
        self._exec(
            "INSERT INTO oauth_codes (code, data, expires_at) VALUES (%s, %s, %s)",
            (code, self._Jsonb(data), expires_at),
        )

    def get_code(self, code):
        row = self._one("SELECT data, expires_at FROM oauth_codes WHERE code = %s", (code,))
        if row is None or row["expires_at"] < time.time():
            return None
        return row["data"]

    def delete_code(self, code):
        self._exec("DELETE FROM oauth_codes WHERE code = %s", (code,))

    def put_token(self, token, kind, data, expires_at):
        self._exec(
            """INSERT INTO oauth_tokens (token, kind, data, expires_at)
               VALUES (%s, %s, %s, %s)""",
            (token, kind, self._Jsonb(data), expires_at),
        )

    def get_token(self, token, kind):
        row = self._one(
            "SELECT data, expires_at FROM oauth_tokens WHERE token = %s AND kind = %s",
            (token, kind),
        )
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            return None
        return row["data"]

    def delete_token(self, token):
        self._exec("DELETE FROM oauth_tokens WHERE token = %s", (token,))


def get_auth_storage() -> AuthStorage:
    if config.backend_name() == "postgres":
        return PostgresAuthStorage()
    return InMemoryAuthStorage()
