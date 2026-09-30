"""API application factory: middleware stack + routers.

Middleware (outer -> inner): request id & timing -> security headers -> CORS -> rate limit ->
audit. Rate limiting and audit key on the authenticated principal when a bearer token is
present (decoded once, cheaply) and fall back to client IP.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

import orjson
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram

from voltpath.api.deps import R
from voltpath.api.routes import public, router
from voltpath.config import settings
from voltpath.metrics import metrics_response

log = logging.getLogger("api")
REQ = Histogram("api_request_seconds", "API latency", ["route", "method", "status"],
                buckets=(.005, .01, .025, .05, .1, .2, .3, .5, 1, 2.5, 5))
RATE_LIMITED = Counter("api_rate_limited_total", "429 responses")

# Fixed-window counter in Redis (atomic INCR + EXPIRE via Lua): O(1), shared across replicas.
RATE_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return c
"""
SKIP_AUDIT = ("/healthz", "/readyz", "/metrics", "/docs", "/openapi.json", "/.well-known")


class AuditWriter:
    """Buffers audit records and writes them in batches off the request path (p95 friendly).
    Queue is bounded; on overflow we log loudly rather than block requests."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=50_000)
        self.task: asyncio.Task | None = None

    def put(self, rec: tuple) -> None:
        try:
            self.q.put_nowait(rec)
        except asyncio.QueueFull:
            log.error("audit queue full - dropping record %s", rec[3])

    async def run(self):
        while True:
            batch = [await self.q.get()]
            while not self.q.empty() and len(batch) < 1000:
                batch.append(self.q.get_nowait())
            try:
                async with R.svc_pool.connection() as c, c.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO audit_log(ts, tenant_id, user_id, actor_type, action, resource, status_code, ip, "
                        "details) VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)", batch)
            except Exception:  # noqa: BLE001
                log.exception("audit write failed (%d records)", len(batch))
            await asyncio.sleep(0.5)

    async def flush(self):
        while not self.q.empty():
            await asyncio.sleep(0.1)


audit_writer = AuditWriter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await R.start()
    audit_writer.task = asyncio.create_task(audit_writer.run())
    app.state.rate_script = R.redis.register_script(RATE_LUA)
    yield
    audit_writer.task.cancel()
    await R.stop()


def create_app() -> FastAPI:
    s = settings()
    app = FastAPI(title="VoltPath API", version="1.0.0", lifespan=lifespan,
                  description="EV charging & battery-health intelligence. All /v1 routes require a bearer JWT "
                              "from POST /v1/auth/token.")

    @app.middleware("http")
    async def audit_mw(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        p = getattr(request.state, "principal", None)
        if p is not None and not path.startswith(SKIP_AUDIT):
            details = {"query": str(request.url.query)[:300], "request_id": request.state.request_id}
            details.update(getattr(request.state, "audit_details", {}))
            audit_writer.put((time.time(), p.tenant_id, p.user_id, p.actor_type,
                              f"{request.method} {request.scope.get('route').path if request.scope.get('route') else path}",
                              path, response.status_code, request.client.host if request.client else None,
                              orjson.dumps(details).decode()))
        return response

    @app.middleware("http")
    async def rate_limit_mw(request: Request, call_next):
        path = request.url.path
        if path.startswith(("/healthz", "/readyz", "/metrics")):
            return await call_next(request)
        ident = request.client.host if request.client else "anon"
        auth = request.headers.get("authorization", "")
        limit = s.rate_limit_per_min
        if path == "/v1/auth/token":
            limit, ident = 20, f"login:{ident}"   # brute-force protection
        elif auth.lower().startswith("bearer "):
            ident = "tok:" + auth[-24:]            # per-token bucket (cheap; token verified later)
        window = int(time.time() // 60)
        try:
            count = await app.state.rate_script(keys=[f"rl:{ident}:{window}"], args=[70])
        except Exception:  # noqa: BLE001 - fail open if Redis is down (graceful degradation)
            count = 0
        if count > limit:
            RATE_LIMITED.inc()
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429,
                                headers={"Retry-After": str(60 - int(time.time()) % 60),
                                         "X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"})
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count))
        return response

    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins.split(","), allow_credentials=False,
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def headers_mw(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"trace_id": rid})
            response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
        dt = time.perf_counter() - t0
        route = request.scope.get("route")
        REQ.labels(route.path if route else "unmatched", request.method, str(response.status_code)).observe(dt)
        h = response.headers
        h["X-Request-ID"] = rid
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        h["Cache-Control"] = "no-store"
        h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if request.url.path.startswith(("/docs", "/openapi.json")):
            del h["Content-Security-Policy"]
        return response

    app.include_router(public)
    app.include_router(router)
    from voltpath.agent.routes import router as copilot_router

    app.include_router(copilot_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        try:
            async with R.pool.connection() as c:
                await c.execute("SELECT 1")
            await R.redis.ping()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"status": "degraded", "error": str(exc)}, status_code=503)
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return metrics_response()

    return app
