"""Runtime configuration. Paths are resolved at call time so OBS_DATA_DIR can
be set per-process (and per-test) without import-order tricks."""

import os
from pathlib import Path

MEMORY_TYPES = {"fact", "decision", "preference", "event", "commitment"}

# Retrieval tuning
RRF_K = 60
MIN_RESULTS_BEFORE_VECTOR = 3  # fallthrough trigger: fewer fused results than this
DEFAULT_RECALL_LIMIT = 10

TOMBSTONE_RETAIN_DAYS = 30

# Vector lane (optional; requires the [vector] extra)
EMBED_MODEL = "BAAI/bge-small-en-v1.5"  # 384-dim, ONNX via fastembed, CPU


def model_dir():
    """Stable model cache shared by CLI, MCP server, and tests — deliberately
    NOT under data_dir(), so scratch/test data dirs don't trigger re-downloads."""
    import os as _os
    from pathlib import Path as _Path
    return _Path(_os.environ.get("OBS_MODEL_DIR", str(_Path.home() / ".openbrainstore" / "models")))


def data_dir() -> Path:
    return Path(os.environ.get("OBS_DATA_DIR", str(Path.home() / ".openbrainstore")))


def blob_dir() -> Path:
    return data_dir() / "blobs"


def db_path() -> Path:
    return data_dir() / "projections.db"


def export_dir() -> Path:
    return data_dir() / "exports"


def user_id() -> str:
    return os.environ.get("OBS_USER", "local")


def backend_name() -> str:
    return os.environ.get("OBS_BACKEND", "sqlite")


def blob_backend() -> str:
    return os.environ.get("OBS_BLOB", "fs")


def s3_bucket() -> str:
    return os.environ.get("OBS_S3_BUCKET", "openbrainstore")


def s3_endpoint() -> str | None:
    # set for R2 (https://<account>.r2.cloudflarestorage.com) or MinIO; unset = AWS
    return os.environ.get("OBS_S3_ENDPOINT")


# ---- OAuth (cloud HTTP transport; unset = auth off, local behavior unchanged) ----

def auth_mode() -> str:
    return os.environ.get("OBS_AUTH", "off")


def issuer_url() -> str:
    # public base URL of this server, e.g. https://memory.example.com
    return os.environ.get("OBS_ISSUER_URL", "").rstrip("/")


def gh_client_id() -> str:
    return os.environ.get("OBS_GH_CLIENT_ID", "")


def gh_client_secret() -> str:
    return os.environ.get("OBS_GH_CLIENT_SECRET", "")


def github_allowed_users() -> set[str]:
    raw = os.environ.get("OBS_GITHUB_ALLOWED_USERS", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


# ---- Quotas & abuse limits ----
# Never throttle writes below an abuse ceiling — friction on `remember`
# breaks the product's core promise. These are ceilings, not soft limits.

def max_memories() -> int:
    return int(os.environ.get("OBS_MAX_MEMORIES", "50000"))


def max_body_bytes() -> int:
    return int(os.environ.get("OBS_MAX_BODY_BYTES", str(64 * 1024)))


def rate_per_min() -> int:
    """Per-token request rate — applies once auth is on (OBS_AUTH=oauth)."""
    return int(os.environ.get("OBS_RATE_PER_MIN", "120"))


def unauth_rate_per_min() -> int:
    """Per-IP rate for unauthenticated requests (OAuth AS endpoints, and any
    /mcp call before the 401)."""
    return int(os.environ.get("OBS_UNAUTH_RATE_PER_MIN", "30"))


def pg_dsn() -> str:
    return os.environ.get("OBS_PG_DSN", "postgresql://obs:obs@localhost:5432/openbrainstore")
