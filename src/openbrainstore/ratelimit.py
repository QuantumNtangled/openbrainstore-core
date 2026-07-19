"""Transport-level rate limiting. Fixed-window counters, keyed by bearer
token when present, else client IP —
distinct from the per-tenant memory-count/body-size quotas in service.py,
which are storage-level ceilings, not request-rate limits.

In-process state: correct for the current single-process deployment. Would
need a shared store (Redis, or Postgres) if the app ever scales to multiple
replicas — noted here so that trigger isn't forgotten."""

import hashlib
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config

_WINDOW_SECONDS = 60


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware:
    """Raw ASGI middleware (not BaseHTTPMiddleware) so it works correctly
    with the streamable-HTTP transport's long-lived/streaming responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._buckets: dict[str, tuple[float, int]] = {}  # key -> (window_start, count)

    def _check(self, key: str, limit: int) -> tuple[bool, int]:
        now = time.monotonic()
        start, count = self._buckets.get(key, (now, 0))
        if now - start >= _WINDOW_SECONDS:
            start, count = now, 0
        count += 1
        self._buckets[key] = (start, count)
        retry_after = max(1, int(_WINDOW_SECONDS - (now - start)))
        # opportunistic cleanup: bound memory growth without a background task
        if len(self._buckets) > 10_000:
            cutoff = now - _WINDOW_SECONDS
            self._buckets = {k: v for k, v in self._buckets.items() if v[0] >= cutoff}
        return count <= limit, retry_after

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):]
            key = "tok:" + hashlib.sha256(token.encode()).hexdigest()[:16]
            limit = config.rate_per_min()
        else:
            key = "ip:" + _client_ip(request)
            limit = config.unauth_rate_per_min()

        allowed, retry_after = self._check(key, limit)
        if not allowed:
            response = JSONResponse(
                {
                    "error": "rate_limited",
                    "error_description": f"too many requests — retry after {retry_after}s",
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
