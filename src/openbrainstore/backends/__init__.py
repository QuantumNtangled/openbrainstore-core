from .base import Backend


def get_backend() -> Backend:
    from .. import config

    name = config.backend_name()
    if name == "postgres":
        from .postgres_backend import PostgresBackend
        return PostgresBackend()
    if name == "sqlite":
        from .sqlite_backend import SqliteBackend
        return SqliteBackend()
    raise ValueError(f"unknown OBS_BACKEND {name!r} (expected sqlite or postgres)")
