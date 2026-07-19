# OpenBrainStore app image: MCP server over streamable HTTP against the
# Postgres backend, with the embedding model baked in at build time so no
# download happens at runtime (per the model policy: never download in-call).
FROM python:3.13-slim-bookworm

# Model cache lives OUTSIDE the data dir so a mounted data volume can't shadow it.
ENV OBS_MODEL_DIR=/opt/obs/models \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

# postgres + s3 + vector extras: the full cloud lane set.
RUN pip install --upgrade pip && pip install ".[postgres,s3,vector]"

# Prefetch the embedding model into OBS_MODEL_DIR (no DB needed for this).
RUN python -c "from openbrainstore import embeddings; print(embeddings.download_model())"

# Drop privileges. Pre-create the data dir owned by obs so a fresh named
# volume mounted here inherits obs ownership (Docker seeds an empty named
# volume from the image path) — otherwise the non-root app can't write to it.
RUN useradd --create-home --uid 10001 obs \
    && mkdir -p /home/obs/.openbrainstore \
    && chown -R obs:obs /home/obs/.openbrainstore /opt/obs
USER obs

EXPOSE 8787

# host 0.0.0.0 so the container is reachable; put a TLS-terminating proxy
# (Caddy) and auth in front — do NOT expose this port directly to the internet.
CMD ["obs", "serve", "--http", "--host", "0.0.0.0", "--port", "8787"]
