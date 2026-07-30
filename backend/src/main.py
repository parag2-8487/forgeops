# SPDX-License-Identifier: FSL-1.1-ALv2
"""ForgeOps backend application factory (design.md §4.3–§4.4, §11.1).

create_app: non-destructive lifespan, unversioned /health and /health/ready,
versioned /api/v1/health. Middleware stack ordering per §4.3.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .ai.rate_limit.redis_bucket import RedisTokenBucketLimiter
from .ai.routes import AIDeps
from .ai.routing.breaker import CircuitBreaker
from .ai.routing.cache import TieredSemanticCache
from .ai.routing.endpoints import EndpointRegistry
from .ai.routing.keys import EnvKeyResolver
from .ai.routing.router import ModelRouter
from .ai.routing.tiers import load_tier_config
from .core.config import get_settings
from .core.db import create_db_engine, create_sessionmaker
from .core.errors import PROBLEM_CONTENT_TYPE, install_problem_handlers
from .core.logging import configure_logging
from .core.middleware import AccessLogMiddleware, RequestIdMiddleware
from .core.trace import TraceContextMiddleware, current_trace_id
from .mcp.apps import McpAppRegistry
from .mcp.auth import OidcTokenVerifier
from .mcp.cache import TtlToolCache
from .mcp.gateway import McpGateway
from .mcp.policy import OpaGatewayPolicy
from .mcp.registry import McpServerRegistry
from .mcp.routing import HeaderRouter
from .mcp.tasks import RedisTaskStore
from .mcp.upstream import McpUpstream


def _resolve_config_path(configured: str) -> Path:
    """Resolve a configured config path against the backend root when relative.

    Extracted rather than repeated: `load_mcp_server_config` already did this
    inline, and two copies of a path rule is how a deployment ends up loading the
    registry from one place and the tier map from another.
    """
    path = Path(configured or "")
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def load_mcp_server_config(settings: Any) -> list[dict[str, Any]]:
    """Load the MCP server registry from the configured YAML path.

    A missing or unreadable registry file yields an EMPTY registry rather than a
    startup failure: with no servers registered every route lookup returns 404,
    which is the correct fail-closed behaviour for a gateway that has nothing to
    route to. An invalid *config value* still fails fast in Settings.
    """
    import logging as _logging

    path = _resolve_config_path(getattr(settings, "mcp_server_registry_path", "") or "")
    if not path.is_file():
        return []

    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - degraded registry, not a crash
        _logging.getLogger(__name__).warning("mcp server registry unreadable", extra={"error": str(exc)})
        return []

    servers = raw.get("servers", [])
    return servers if isinstance(servers, list) else []


logger = logging.getLogger(__name__)


async def _check_postgres(engine: Any) -> None:
    """Quick PostgreSQL check with a 2s timeout."""
    from sqlalchemy import text as sa_text

    async with engine.connect() as conn:
        await conn.execute(sa_text("SELECT 1"))


async def _check_redis(redis: Any) -> None:
    """Quick Redis check with a 2s timeout."""
    await redis.ping()


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """Non-destructive lifespan (§4.4, §11.1).

    Construction validates local configuration but performs no mandatory
    network handshake. pool_pre_ping/lazy clients reconnect on first use.
    The lifespan does NOT abort startup solely because PostgreSQL or Redis
    is unreachable and does NOT contain eager startup-failing SELECT 1 or
    Redis PING. Best-effort probes with a 2s timeout only log warnings.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    # Construct engine/session — validates URL shape, does not connect.
    engine = create_db_engine(settings)
    sessionmaker = create_sessionmaker(engine)

    # Construct Redis client — no eager PING.
    redis_client = aioredis.from_url(
        str(settings.redis_url),
        decode_responses=True,
        socket_connect_timeout=2.0,
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.redis = redis_client

    # --- MCP Gateway collaborators (§11.1, §11.4) ---------------------------
    # All constructed non-destructively: the shared HTTP client, the JWKS-caching
    # verifier, the OPA client and the Redis-backed cache/task store each validate
    # local configuration only. None of them performs a network handshake here, so
    # an unreachable OPA or Redis changes readiness, not process liveness.
    shared_http = httpx.AsyncClient(timeout=settings.outbound_http_timeout_seconds)

    mcp_registry = McpServerRegistry.from_config(load_mcp_server_config(settings))
    mcp_verifier = OidcTokenVerifier(
        allowed_issuers=settings.mcp_oidc_issuer_list,
        audience=settings.mcp_oidc_audience,
        jwks_ttl_seconds=settings.mcp_oidc_jwks_ttl_seconds,
        http=shared_http,
    )
    mcp_policy = OpaGatewayPolicy(opa_url=str(settings.opa_url), http=shared_http)
    mcp_cache = TtlToolCache(redis_client, max_ttl_ms=settings.mcp_cache_max_ttl_ms)
    mcp_upstream = McpUpstream(http=shared_http)
    mcp_task_store = RedisTaskStore(redis_client)

    app.state.shared_http = shared_http
    app.state.mcp_registry = mcp_registry
    app.state.mcp_verifier = mcp_verifier
    app.state.mcp_task_store = mcp_task_store
    app.state.mcp_app_registry = McpAppRegistry()
    app.state.mcp_gateway = McpGateway(
        registry=mcp_registry,
        verifier=mcp_verifier,
        router=HeaderRouter(mcp_registry),
        policy=mcp_policy,
        cache=mcp_cache,
        upstream=mcp_upstream,
        agent_blast_radius=settings.mcp_agent_blast_radius,
    )

    # ── debt D1: the shipped YAML is now what a running backend loads ─────────
    # Phase 0 defined load_tier_config(path, env) and never called it from
    # production, so config/model-tiers.yaml was only ever exercised by fixtures
    # (PROGRESS.md outstanding item, design §0.5 D1). §1.5's entire generation
    # pipeline sits on six-tier routing, so this wiring lands BEFORE any generation
    # code and is proven by Q-27 against the RUNNING app rather than by reading the
    # file.
    #
    # A malformed or missing tier file is a startup failure, deliberately unlike
    # the MCP registry above. An empty MCP registry fails closed — every route
    # returns 404 — but an empty tier map would leave `/api/v1/ai/complete`
    # answering 422 for every tier while looking healthy, which is precisely the
    # silent-degradation shape D1 exists to remove.
    tier_config = load_tier_config(_resolve_config_path(settings.model_tier_config_path), env=os.environ)
    endpoint_registry = EndpointRegistry.from_config(tier_config, http=shared_http)
    breakers = {
        endpoint_id: CircuitBreaker(
            failure_threshold=settings.cb_failure_threshold,
            failure_window_seconds=float(settings.cb_window_seconds),
            cooldown_seconds=float(settings.cb_open_seconds),
        )
        for endpoint_id in tier_config.endpoints
    }
    semantic_cache = TieredSemanticCache(redis=redis_client)
    model_router = ModelRouter(
        tier_config=tier_config,
        registry=endpoint_registry,
        cache=semantic_cache,
        breakers=breakers,
        key_resolver=EnvKeyResolver(),
    )

    # Exposed so Q-27 can assert provenance against the running app.
    app.state.tier_config = tier_config
    app.state.endpoint_registry = endpoint_registry
    app.state.breakers = breakers
    app.state.semantic_cache = semantic_cache
    app.state.model_router = model_router

    # `ai/routes.py` reads `app.state.ai_deps` and the Phase 0 lifespan never set
    # it, so every request to `/api/v1/ai/tiers` and `/api/v1/ai/complete` raised
    # AttributeError. That is the same class of defect as D-23: a registered route
    # whose composition was never assembled, reported as live.
    #
    # The verifier is Phase 0's MCP verifier for now, which means the AI routes
    # currently accept the gateway's audience. Task 6.1 replaces it with
    # `AppTokenVerifier`, which has a DISTINCT app audience; until then this is the
    # Phase 0 contract wired up rather than a new one invented here.
    app.state.ai_deps = AIDeps(
        tier_config=tier_config,
        registry=endpoint_registry,
        breakers=breakers,
        model_router=model_router,
        limiter=RedisTokenBucketLimiter(
            redis=redis_client,
            capacity=settings.ai_rate_limit_capacity,
            refill_rate=settings.ai_rate_limit_refill_per_second,
        ),
        verifier=mcp_verifier,
    )

    # Best-effort initial observations: dependency outage changes readiness, not
    # process liveness.
    for name, probe in [
        ("postgres", lambda: _check_postgres(engine)),
        ("redis", lambda: _check_redis(redis_client)),
    ]:
        try:
            await asyncio.wait_for(probe(), timeout=2.0)
        except Exception as exc:
            logger.warning(
                "dependency unavailable during startup",
                extra={"dependency": name, "error": str(exc)},
            )

    logger.info(
        "startup complete",
        extra={"env": settings.app_env, "version": settings.service_version},
    )
    try:
        yield
    finally:
        await shared_http.aclose()
        await redis_client.aclose()
        await engine.dispose()
        logger.info("shutdown complete")


def create_app() -> FastAPI:
    """Application factory (§11.1)."""
    settings = get_settings()
    app = FastAPI(
        title="ForgeOps API",
        version=settings.service_version,
        lifespan=lifespan,
        openapi_url=f"{settings.api_prefix}/openapi.json",
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=None,
        default_response_class=ORJSONResponse,
    )

    # Starlette PREPENDS middleware, so registration order is the REVERSE of
    # execution order. Registering innermost-first yields the §4.3 stack:
    # ServerError(1) -> RequestId(2) -> TraceContext(3) -> AccessLog(4) -> CORS(5)
    cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )  # executes 5th
    app.add_middleware(AccessLogMiddleware)  # executes 4th
    app.add_middleware(TraceContextMiddleware)  # executes 3rd
    app.add_middleware(RequestIdMiddleware)  # executes 2nd
    # ServerErrorMiddleware is installed by Starlette outermost — executes 1st.

    install_problem_handlers(app)

    # --- API Routers ---
    from .analysis.routes import router as analysis_router

    app.include_router(analysis_router)

    from .ai.routes import router as ai_router

    app.include_router(ai_router)

    # MCP Gateway ingress, registry introspection and App hosting (§5.2).
    from .mcp.routes import router as mcp_router

    app.include_router(mcp_router, prefix=settings.api_prefix)

    # --- Health endpoints (unversioned = infrastructure contract) ---
    @app.get("/health")
    async def health() -> JSONResponse:
        """Liveness. Event loop accepts work. No dependency I/O."""
        return JSONResponse(
            {
                "status": "ok",
                "version": settings.service_version,
                "commit": settings.git_commit,
            }
        )

    @app.get("/health/ready")
    async def health_ready(request: Request) -> JSONResponse:
        """Readiness. PostgreSQL SELECT 1 + Redis PING, each with 2s timeout."""
        errors: list[dict[str, str]] = []

        # PostgreSQL check
        try:
            await asyncio.wait_for(_check_postgres(request.app.state.engine), timeout=2.0)
        except TimeoutError:
            errors.append({"dependency": "postgres", "detail": "health check timed out"})
        except Exception as exc:
            errors.append({"dependency": "postgres", "detail": str(exc)})

        # Redis check
        try:
            await asyncio.wait_for(_check_redis(request.app.state.redis), timeout=2.0)
        except TimeoutError:
            errors.append({"dependency": "redis", "detail": "health check timed out"})
        except Exception as exc:
            errors.append({"dependency": "redis", "detail": str(exc)})

        if errors:
            # Rendered with the same required RFC 9457 members as every other
            # problem document (§4.2): stable type URI, title, status equal to
            # the HTTP status, the offending path as instance, and the current
            # trace id for correlation.
            return JSONResponse(
                status_code=503,
                content={
                    "type": "https://errors.forgeops.dev/not-ready",
                    "title": "Service not ready",
                    "status": 503,
                    "detail": "One or more dependencies are unavailable.",
                    "instance": request.url.path,
                    "trace_id": current_trace_id(),
                    "errors": errors,
                },
                media_type=PROBLEM_CONTENT_TYPE,
            )

        return JSONResponse({"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}})

    # --- Versioned health (API surface informational echo) ---
    @app.get(f"{settings.api_prefix}/health")
    async def api_v1_health() -> JSONResponse:
        """Versioned informational echo of liveness, also dependency-free."""
        return JSONResponse(
            {
                "status": "ok",
                "version": settings.service_version,
                "commit": settings.git_commit,
            }
        )

    return app
