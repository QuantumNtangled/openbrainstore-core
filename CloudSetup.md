# Cloud setup

Self-host OpenBrainStore reachable from anywhere — your MCP client, your
phone, a friend's — with real TLS and GitHub sign-in. ~15 minutes.

Don't need public access — just yourself, or a few devices on your home
network? See [`LocalSetup.md`](LocalSetup.md) instead; it's faster and skips
the domain and TLS entirely.

## You'll need

- A server **reachable from the public internet on ports 80 and 443**, with
  **Docker** and the **Compose plugin** installed. In practice this means a
  cheap VPS (Hetzner, DigitalOcean, etc.) — a home machine or a WSL box
  usually **won't** satisfy this out of the box, since it typically sits
  behind NAT, a router with no port forwarding, and often an ISP that doesn't
  hand out a real public IP at all. (WSL runs the software itself just fine —
  Docker, Postgres, and Caddy all work there — it's specifically the "is this
  address reachable from the internet" part that a rented VPS solves and a
  home network usually doesn't.)
- A **hostname that resolves to that server's IP** — a real domain, or a free
  wildcard-DNS placeholder like `<dashed-ip>.sslip.io` (e.g. `203-0-113-10.sslip.io`
  resolves to `203.0.113.10` with zero setup). Caddy provisions a real Let's
  Encrypt certificate for it automatically.
- A **GitHub account** — OpenBrainStore uses GitHub as its identity provider
  (see [OKF.md](OKF.md)); you'll create a small OAuth App for it.

## 1. Clone and configure

```bash
git clone https://github.com/QuantumNtangled/openbrainstore-core.git
cd openbrainstore-core
cp docker-compose.example.yml docker-compose.yml
cp .env.example .env
```

Point your domain (or sslip.io hostname) at the server now, if you haven't —
DNS needs a few minutes to propagate before step 3's certificate request.

## 2. Create a GitHub OAuth App

GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App**:

| Field | Value |
|---|---|
| Homepage URL | `https://<your-domain>` |
| Authorization callback URL | `https://<your-domain>/github/callback` |

Save the **Client ID** and generate a **Client secret**.

## 3. Fill in `.env`

```bash
POSTGRES_PASSWORD=<openssl rand -hex 32>
OBS_DOMAIN=<your-domain>
OBS_GH_CLIENT_ID=<from step 2>
OBS_GH_CLIENT_SECRET=<from step 2>
OBS_GITHUB_ALLOWED_USERS=<your-github-username>
```

`OBS_GITHUB_ALLOWED_USERS` is the entire access-control policy — only these
comma-separated GitHub logins can authenticate. Leave `OBS_PG_USER` commented
out for now (see [Harden](#harden-enable-row-level-security) below).

## 4. Start it

```bash
docker compose up -d --build
docker compose ps        # all three services should be "Up" / "healthy"
```

`--build` builds the app image from this repo's `Dockerfile` on your server —
no container registry involved, works on a fresh clone with nothing else set
up. (Already publishing images via this repo's `publish.yml` workflow? Set
`OBS_IMAGE` in `.env` to your published tag, run `docker compose pull`
instead, and drop `--build`.) First start takes a couple of minutes: the
build installs dependencies and prefetches the embedding model, then Caddy
requests your certificate.

## 5. Verify

```bash
curl https://<your-domain>/.well-known/oauth-authorization-server   # OAuth metadata (200)
curl -i https://<your-domain>/mcp                                    # 401 + WWW-Authenticate
```

A `401` on `/mcp` with no `-k`/cert warning means TLS, routing, and the app
are all working — that's the expected response to an unauthenticated request.

## 6. Connect a client

Any MCP client that supports OAuth 2.1 with dynamic client registration:

- **Claude (desktop or web):** Settings → Connectors → Add custom connector →
  URL `https://<your-domain>/mcp` → leave Client ID/Secret empty → sign in
  with GitHub.
- **Other MCP clients:** point them at `https://<your-domain>/mcp`; OAuth
  discovery handles the rest.

If your GitHub login isn't in `OBS_GITHUB_ALLOWED_USERS`, the sign-in
completes but you'll get a 403 — add it to `.env` and
`docker compose up -d` again.

---

## Harden: enable row-level security

By default the app connects to Postgres as the bootstrap **superuser**, which
works fine but bypasses row-level security as a defense-in-depth backstop
(see [`docs/specs/tenant-isolation.md`](docs/specs/tenant-isolation.md)).
Recommended before inviting more than yourself.

Run once, connected as the superuser:

```bash
docker compose exec postgres psql -U obs -d openbrainstore
```

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE ROLE obs_app LOGIN PASSWORD '<same value as POSTGRES_PASSWORD in .env>' NOSUPERUSER;
GRANT ALL ON SCHEMA public TO obs_app;
```

Then uncomment `OBS_PG_USER=obs_app` in `.env` and restart:

```bash
docker compose up -d
```

The app owns every table it creates under `obs_app`, so it self-provisions
the rest of its schema — including the RLS policies — on that first connect.
Verify it took effect:

```bash
docker compose exec postgres psql -U obs_app -d openbrainstore -c "SELECT count(*) FROM memories"
```

`0` with no memories written under this role yet is correct — fail-closed by
design (an undeclared tenant sees nothing).

## Backups

`ops/backup-postgres.sh` does a nightly `pg_dump` to any S3-compatible
storage (works with Cloudflare R2, AWS S3, MinIO, etc.) with 14-daily +
4-weekly retention and automatic pruning. Set `OBS_BACKUP_S3_BUCKET`,
`OBS_BACKUP_S3_ENDPOINT`, and standard `AWS_ACCESS_KEY_ID`/
`AWS_SECRET_ACCESS_KEY` in `.env`, then install the included systemd timer:

```bash
sudo install -m 644 ops/openbrainstore-backup.service ops/openbrainstore-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openbrainstore-backup.timer
```

**Test the restore before you need it** — an unverified backup is a
hypothesis, not a backup:

```bash
docker compose exec postgres psql -U obs -d postgres -c "CREATE DATABASE openbrainstore_scratch"
aws s3 cp s3://<bucket>/daily/<latest>.dump.gz - --endpoint-url <endpoint> \
  | gunzip | docker compose exec -T postgres pg_restore -U obs -d openbrainstore_scratch --clean --if-exists
docker compose exec postgres psql -U obs -d openbrainstore_scratch -c "SELECT count(*) FROM memories"
docker compose exec postgres psql -U obs -d postgres -c "DROP DATABASE openbrainstore_scratch"
```

## Updating

Building from source (the default in step 4):

```bash
git pull
docker compose up -d --build
```

Pulling a published image instead (`OBS_IMAGE` set in `.env`):

```bash
docker compose pull
docker compose up -d
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Certificate request hangs / fails | DNS for `OBS_DOMAIN` hasn't propagated, or ports 80/443 aren't reachable from the internet |
| `role "obs_app" does not exist` | You uncommented `OBS_PG_USER` before running the [Harden](#harden-enable-row-level-security) step |
| `403` after GitHub sign-in | Your GitHub login isn't in `OBS_GITHUB_ALLOWED_USERS` |
| `421 Misdirected Request` on `/mcp` | `OBS_ALLOWED_HOSTS` doesn't match the domain you're connecting through (it's set from `OBS_DOMAIN` automatically — check for a typo or a proxy in front of Caddy) |

See [`docs/specs/`](docs/specs) for the design behind tenant isolation and the
OKF export format, and [`OKF.md`](OKF.md) for the canonical file format.
