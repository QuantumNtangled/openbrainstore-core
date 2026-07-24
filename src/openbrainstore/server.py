"""MCP server (stdio). The tool schema is the contract — descriptions must
carry everything an agent needs, because we don't control the calling agent."""

import os

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import config, service
from .backends import get_backend
from .recall import recall as run_recall


def _transport_security() -> TransportSecuritySettings | None:
    """Behind a reverse proxy the app sees the public hostname in the Host
    header; the streamable-HTTP transport's DNS-rebinding protection rejects it
    (421) unless it's allowlisted. OBS_ALLOWED_HOSTS (comma-separated) names the
    host(s) we're legitimately served as. Unset -> SDK default (localhost only),
    which is correct for local stdio/HTTP use."""
    allowed = [h.strip() for h in os.environ.get("OBS_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not allowed:
        return None
    origins = [f"https://{h}" for h in allowed] + [f"http://{h}" for h in allowed]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
        allowed_origins=origins,
    )


def _auth_components():
    """OBS_AUTH=oauth turns this server into its own OAuth 2.1 authorization
    server (SDK mounts /authorize, /token, /register + metadata; PKCE handled
    by the SDK) with GitHub as the upstream identity check. Unset -> no auth,
    which is correct for local stdio/loopback use."""
    if config.auth_mode() != "oauth":
        return None, None
    issuer = config.issuer_url()
    if not issuer:
        raise RuntimeError("OBS_AUTH=oauth requires OBS_ISSUER_URL")
    if not config.gh_client_id() or not config.gh_client_secret():
        raise RuntimeError("OBS_AUTH=oauth requires OBS_GH_CLIENT_ID and OBS_GH_CLIENT_SECRET")

    from .auth.provider import GitHubOAuthProvider
    from .auth.storage import get_auth_storage

    provider = GitHubOAuthProvider(get_auth_storage(), issuer, config.github_allowed_users())
    auth = AuthSettings(
        issuer_url=issuer,
        resource_server_url=f"{issuer}/mcp",
        required_scopes=["memory"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["memory"], default_scopes=["memory"],
        ),
    )
    return provider, auth


_provider, _auth_settings = _auth_components()

mcp = FastMCP(
    "openbrainstore",
    transport_security=_transport_security(),
    auth_server_provider=_provider,
    auth=_auth_settings,
)

if _provider is not None:
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, RedirectResponse

    from .auth.provider import DeniedUserError

    @mcp.custom_route("/github/callback", methods=["GET"])
    async def github_callback(request: Request):
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return PlainTextResponse("missing code or state", status_code=400)
        try:
            redirect = await _provider.handle_github_callback(code, state)
        except DeniedUserError:
            return PlainTextResponse(
                "This GitHub account is not authorized for this memory server.",
                status_code=403,
            )
        except ValueError:
            return PlainTextResponse("unknown or expired authorization request", status_code=400)
        return RedirectResponse(redirect, status_code=302)


# Public beta-access signup for the website — registered regardless of auth
# mode (it's an unauthenticated endpoint; the rate limiter still applies in
# HTTP mode). Blocking DB work is offloaded so it doesn't stall the loop.
@mcp.custom_route("/beta-signup", methods=["POST"])
async def beta_signup(request):
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    from . import web_signup

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Expected a JSON body."}, status_code=400)

    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)

    try:
        await run_in_threadpool(web_signup.submit, data, ip)
    except web_signup.SignupError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "Something broke on our end — try again in a moment."},
            status_code=500,
        )
    return JSONResponse({"ok": True})


@mcp.custom_route("/contact", methods=["POST"])
async def contact(request):
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    from . import web_contact

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Expected a JSON body."}, status_code=400)

    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)

    try:
        await run_in_threadpool(web_contact.submit, data, ip)
    except web_contact.ContactError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "Something broke on our end — try again in a moment."},
            status_code=500,
        )
    return JSONResponse({"ok": True})


def _current_user() -> str:
    """User identity for this request: the OAuth token subject when auth is on
    (identity-scoped tenancy per the spec), else the configured local user."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        token = get_access_token()
        if token is not None and token.subject:
            return token.subject
    except Exception:
        pass
    return config.user_id()


@mcp.tool()
def remember(
    content: str,
    type: str,
    entities: list[str] | None = None,
    tags: list[str] | None = None,
    kv: dict | None = None,
    links: list[str] | None = None,
) -> dict:
    """Store a long-term memory. Call this whenever you learn something durable
    about the user, a project, or a decision that future sessions should know.

    Args:
        content: The memory itself, written as clean Markdown. One fact per
            memory — atomicity first: a short memory is one or two plain
            sentences, no formatting needed. When a memory genuinely needs
            more (a plan, a checkpoint, a multi-part decision), structure it:
            **bold** the key terms, use short `-` bullet lists, keep
            paragraphs brief. Never one unbroken wall of prose — memories are
            read back by humans as well as agents.
        type: One of: fact | decision | preference | event | commitment.
        entities: People/projects/things this memory is about (e.g. ["project-alpha", "sarah"]).
        tags: Topical tags (e.g. ["architecture", "postgres"]).
        kv: Optional structured key-value pairs (e.g. {"project": "okf", "priority": "high"}).
            Call get_memory_schema first and REUSE existing keys rather than inventing
            near-duplicates; keys are normalized to snake_case automatically.
        links: Ids of existing related memories (e.g. ["mem_01J9..."]) — connects this
            memory into the knowledge graph so recall can surface related context.
            Every id must belong to you; use ids returned by recall or remember.

    Returns the stored memory's id and its normalized fields.
    """
    with get_backend() as backend:
        return service.remember(backend, content, type, entities, tags, kv, links,
                                source_harness="mcp", user=_current_user())


@mcp.tool()
def update(
    id: str,
    content: str | None = None,
    type: str | None = None,
    entities: list[str] | None = None,
    tags: list[str] | None = None,
    kv: dict | None = None,
    links: list[str] | None = None,
) -> dict:
    """Revise an existing memory — for corrections, status changes, and
    reformatting. The id, created date, and every inbound link survive, so
    prefer this over forget()+remember() when a memory is wrong or stale.
    Discipline: a genuinely NEW fact deserves its own remember() linked to
    this one — don't edit away what was true when it was written.

    Args:
        id: The memory to revise (an id returned by recall or remember).
        content: Replacement body — clean Markdown, same guidance as remember.
        type: Replacement type (fact | decision | preference | event | commitment).
        entities / tags / kv: Replacement values. Omitted fields keep their
            current values; passing a value REPLACES that field entirely.
        links: REPLACES the outgoing link set (link() appends instead).

    Returns the revised memory plus a `changed` list naming what moved
    (empty if the update was a no-op).
    """
    with get_backend() as backend:
        return service.update(
            backend, id, content=content, type=type, entities=entities,
            tags=tags, kv=kv, links=links, user=_current_user(),
        )


@mcp.tool()
def recall(
    query: str | None = None,
    type: str | None = None,
    entities: list[str] | None = None,
    tags: list[str] | None = None,
    kv: dict | None = None,
    since: str | None = None,
    until: str | None = None,
    depth: int = 1,
    deep: bool = False,
    limit: int = 10,
    sort: str = "relevance",
) -> dict:
    """Retrieve memories. Runs a lane cascade: structured filters and full-text
    search first, graph expansion for related context, and a semantic vector
    search only as a fallback (or when deep=true).

    Args:
        query: Free-text search over memory bodies.
        type: Filter to one of: fact | decision | preference | event | commitment.
        entities: Filter/expand by entities (e.g. ["project-alpha"]).
        tags: Filter by tags (any match).
        kv: Filter by structured key-values (near-miss keys are fuzzy-matched).
        since / until: ISO date bounds on creation time (e.g. "2026-01-01");
            bounds apply across every lane, including full-text and vector.
        depth: Graph expansion hops, 1 or 2.
        deep: Force the semantic vector lane when your own confidence is low.
        limit: Max results.
        sort: "relevance" (default), "newest", or "oldest". With no query or
            filters at all, sort="newest" browses your latest memories —
            e.g. sort="newest", limit=1 returns the last memory you stored.

    Returns results with ids, types, scores, bodies, and which lanes matched,
    plus lane instrumentation.
    """
    filters: dict = {}
    if type:
        filters["type"] = type
    if tags:
        filters["tags"] = tags
    if kv:
        filters["kv"] = kv
    if since:
        filters["since"] = since
    if until:
        filters["until"] = until
    with get_backend() as backend:
        return run_recall(
            backend, _current_user(), query=query, filters=filters,
            entities=entities, depth=depth, deep=deep, limit=limit, sort=sort,
        )


@mcp.tool()
def get_memory_schema() -> dict:
    """Return the user's live memory vocabulary: distinct kv keys with top
    values, known entities, tags, and per-type counts. Call this BEFORE writing
    memories and reuse existing keys/entities/tags — this keeps the vocabulary
    convergent instead of drifting."""
    with get_backend() as backend:
        return service.get_memory_schema(backend, user=_current_user())


@mcp.tool()
def link(id: str, to: list[str]) -> dict:
    """Link an existing memory to other related memories after the fact.
    Use when you notice two memories belong together (a decision and the event
    that revisited it, a fact and the project it concerns). Links power the
    graph lane of recall: querying one memory surfaces its linked context.

    Args:
        id: The memory to add links to.
        to: Ids of existing memories to link it with. All must be yours.

    Returns the memory's id and its full link list. Idempotent — already-linked
    ids are kept once.
    """
    with get_backend() as backend:
        return service.link(backend, id, to, user=_current_user())


@mcp.tool()
def forget(id: str) -> dict:
    """Delete a memory by id. Projections are removed immediately; the canonical
    file is tombstoned (retained for a grace period, then purged)."""
    with get_backend() as backend:
        return service.forget(backend, id, user=_current_user())


@mcp.tool()
def export(raw: bool = False) -> dict:
    """Export all memories as a tar.gz and return its local path — your memory
    is markdown you can take with you.

    By default this is a browsable Open Knowledge Format bundle: one clean
    Markdown file per memory, related memories as links, index and per-entity
    pages, and your identity stripped so it's safe to share. Set raw=true for
    the full-fidelity internal dump (every version, exactly as stored)."""
    return service.export(user=_current_user(), profile="raw" if raw else "okf")


def main(http: bool = False, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run the MCP server. stdio by default (local harnesses spawn us as a
    subprocess); streamable HTTP for remote/cloud use.

    Security note: without OBS_AUTH=oauth, HTTP mode has NO authentication —
    bind loopback only. Exposing it publicly unauthenticated would hand your
    memory store to anyone who can reach the port."""
    if not http:
        mcp.run()
        return

    mcp.settings.host = host
    mcp.settings.port = port

    if config.auth_mode() != "oauth":
        mcp.run(transport="streamable-http")
        return

    # Same as FastMCP.run_streamable_http_async(), plus rate-limit middleware.
    # Reimplemented (rather than passed to mcp.run) because the SDK gives no
    # hook to add middleware to the app it builds internally.
    import anyio
    import uvicorn

    from .ratelimit import RateLimitMiddleware

    async def _serve() -> None:
        app = mcp.streamable_http_app()
        app.add_middleware(RateLimitMiddleware)
        uv_config = uvicorn.Config(
            app, host=mcp.settings.host, port=mcp.settings.port,
            log_level=mcp.settings.log_level.lower(),
        )
        await uvicorn.Server(uv_config).serve()

    anyio.run(_serve)


if __name__ == "__main__":
    main()
