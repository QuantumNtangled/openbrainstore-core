# Local setup

For your own machine, or a few devices on a network you trust (home, small
office). No domain, no TLS, no GitHub OAuth app — minutes, not fifteen.

Need it reachable from anywhere, or want real per-person identity via GitHub
sign-in? See [`CloudSetup.md`](CloudSetup.md) instead.

## Install

```bash
git clone https://github.com/QuantumNtangled/openbrainstore-core.git
cd openbrainstore-core
python -m venv .venv
.venv/bin/pip install -e .          # Linux/macOS
# .venv\Scripts\pip install -e .    # Windows
```

No Docker, no Postgres — by default OpenBrainStore uses **SQLite**, a single
file at `~/.openbrainstore`. (Prefer Postgres? Set `OBS_BACKEND=postgres` and
`OBS_PG_DSN`; everything below still applies.)

## Tier 1 — just for you (recommended default)

Your MCP client spawns `obs` directly and talks to it over stdio — no network
port opens at all.

```bash
.venv/bin/obs serve
```

Point your client's MCP config at the `obs` executable with `serve` as its
argument (check your client's docs for the exact config format — this is the
same "local MCP server" pattern most clients already support for any tool).

That's it. `remember`, `recall`, and the rest are available immediately, with
no auth needed since nothing is exposed over the network.

## Tier 2 — share it on your LAN

Want more than one device (or person) on your home network to reach the same
memory? Run it over HTTP instead:

```bash
.venv/bin/obs serve --http --host 0.0.0.0 --port 8787
```

Then, on the box itself, allow the hostname/IP other devices will actually
connect through — **required**, or non-localhost requests get rejected with
`421 Misdirected Request`:

```bash
OBS_ALLOWED_HOSTS=192.168.1.50 .venv/bin/obs serve --http --host 0.0.0.0 --port 8787
```

(Replace `192.168.1.50` with this machine's LAN IP, or a LAN hostname if you
have one. Comma-separate multiple.)

Other devices connect an MCP client to `http://192.168.1.50:8787/mcp`.

> **Know what this gives up.** There's no TLS (plain HTTP) and no
> authentication (`OBS_AUTH` is unset) — every request shares one identity
> (`OBS_USER`, default `local`), and anything that can reach the port can
> read and write every memory. That's the right trade for a home network you
> control; it is **not** safe on a network with people or devices you don't
> trust (open WiFi, a shared office LAN, etc.). For real per-person identity
> and encryption, use [`CloudSetup.md`](CloudSetup.md) — the OAuth and TLS
> machinery is the same code either way, it's just off by default locally.

## Running it in the background

`obs serve` is a plain foreground process. To keep it running:

- **Linux/macOS:** a `systemd --user` unit, or `tmux`/`screen`, or a
  supervisor like `pm2`.
- **Windows:** Task Scheduler, or run it in a persistent terminal.

## Optional: semantic search

The base install gives you the full retrieval cascade — structured filters,
full-text search, and graph traversal — with no extra setup. What it doesn't
include is the **vector lane**, the semantic-similarity fallback that only
fires when those first three come up empty (or when a client explicitly asks
for `deep` recall). That's opt-in, in two steps:

```bash
.venv/bin/pip install -e ".[vector]"
.venv/bin/obs embed
```

`obs embed` downloads and caches the embedding model (~130 MB, one-time,
from Hugging Face) and backfills embeddings for anything already stored.
**This is the only command that ever downloads it** — a live `recall` or
`remember` call will never trigger a multi-minute download mid-request; if
the model isn't cached yet, the vector lane just silently sits out and you
still get structured/full-text/graph results. Skip both steps entirely and
nothing else changes — semantic fallback just never fires.

## Next steps

- `obs schema` shows your live vocabulary (entity/tag/kv-key names) so new
  writes reuse them instead of drifting.
- `obs export` produces a browsable [OKF](OKF.md) bundle you can read, diff,
  or move elsewhere at any time — the portability promise applies locally too.
- See the main [README](README.md#what-it-is) for how retrieval and the
  canonical format work.
