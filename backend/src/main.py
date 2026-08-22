# SPDX-License-Identifier: FSL-1.1-ALv2
"""ForgeOps backend application factory (design.md §4.3–§4.4, §11.1).

create_app: non-destructive lifespan, unversioned /health and /health/ready,
versioned /api/v1/health. Middleware stack ordering per §4.3.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .ai.embeddings import EmbeddingOrchestrator
from .ai.rate_limit.redis_bucket import RedisTokenBucketLimiter
from .ai.routes import AIDeps
from .ai.routing.breaker import CircuitBreaker
from .ai.routing.cache import TieredSemanticCache
from .ai.routing.endpoints import EndpointRegistry
from .ai.routing.keys import EnvKeyResolver
from .ai.routing.router import ModelRouter
from .ai.routing.tiers import load_tier_config
from .analysis.plan_analyzer.approval import ThresholdApprovalGate
from .analysis.plan_analyzer.semantic import SemanticPlanAnalyzer
from .audit.writer import AuditWriter
from .auth.ca import InternalCertificateAuthority, UnavailableCertificateAuthority
from .auth.cerbos import CerbosClient
from .auth.devices import REVOCATION_CHANNEL, DeviceService
from .auth.oidc import IdTokenVerifier, OidcClient
from .auth.pairing_limits import TokenBucketPairingLimiter
from .auth.sessions import SessionService
from .auth.verifier import AppTokenVerifier
from .core.config import Settings, get_settings
from .core.db import create_db_engine, create_sessionmaker
from .core.errors import PROBLEM_CONTENT_TYPE, install_problem_handlers
from .core.logging import configure_logging
from .core.middleware import AccessLogMiddleware, RequestIdMiddleware
from .core.tenancy import TenantContextMiddleware
from .core.trace import TraceContextMiddleware, current_trace_id
from .governance.chokepoint import GovernanceChokepoint
from .governance.device_audit import GovernanceDeviceAuditRecorder
from .governance.policy import (
    UnavailableGovernancePolicy,  # noqa: F401 - re-exported for tests and deployments with no bundle
)
from .governance.sequencing import RedisEnvelopeSequencer
from .mcp.apps import McpAppRegistry
from .mcp.auth import OidcTokenVerifier
from .mcp.cache import TtlToolCache
from .mcp.gateway import McpGateway
from .mcp.policy import OpaGatewayPolicy
from .mcp.registry import McpServerRegistry
from .mcp.routing import HeaderRouter
from .mcp.tasks import RedisTaskStore
from .mcp.upstream import McpUpstream
from .policies.opa import OpaGovernancePolicy
from .websocket.hub import AgentHub, HubDeps, RedisProgressSink, TlsPeerCertificate


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


def _build_cache_embedder(settings: Settings) -> Callable[[str], Awaitable[Sequence[float]]] | None:
    """The L2 embedder for the semantic cache, or `None` to leave the cache L1-only.

    Criterion 14's L2 tier existed in `ai/routing/cache.py`, with tests, and was never
    constructed here — so the deployed backend ran L1 only while the record claimed the
    criterion met (LEARNING-JOURNAL finding 79). This is the wiring that closes it, and
    `test_wiring_tier_config.py` asserts on the constructed object so it cannot silently
    come undone again.

    WHY THIS REFUSES TO ENABLE L2 WITHOUT A REAL KEY, WHICH IS NOT MERE CAUTION
    ---------------------------------------------------------------------------
    `EmbeddingOrchestrator.generate_embedding` falls back to

        mock_vector = [0.01 * (i % 100) for i in range(1024)]

    on every path that is not Voyage-with-a-real-key — including `bge_m3`, whose local
    model is not implemented yet. That vector **does not depend on its input**: two
    unrelated prompts embed to the identical vector, so their cosine similarity is 1.0.
    Enabling L2 over it would make every prompt a near-duplicate of every other prompt
    and the cache would serve an arbitrary stored completion for any question asked.
    Verified rather than assumed: `generate_embedding("...one")` and
    `generate_embedding("...two")` return equal vectors.

    So L2 is enabled only when the embedder is input-sensitive. Absent that, the cache
    stays exact-match, which is correct and safe rather than degraded — and the startup
    log names which tier is live so an operator is never guessing.
    """
    if settings.embedding_backend != "voyage":
        # `bge_m3` selects the local table (D-48) but has no local model yet, so it
        # returns the input-independent fallback. Not usable as a similarity key.
        return None

    voyage_key = settings.llm_key_voyage.get_secret_value()
    if not voyage_key or voyage_key == "placeholder":
        # `.env.example` ships `LLM_KEY_VOYAGE=placeholder`, which the orchestrator
        # treats as "no key" and answers from the fallback. A fresh clone therefore gets
        # L1 only, deliberately.
        return None

    # Positional, and the local is `voyage_key` rather than the parameter's own name, because
    # that name is a credential SHAPE the pre-commit gate refuses in an added line
    # (FO-SEC001, `.antigravity/steering/secret-safety.md`). The rule is shape rather than
    # sensitivity — this is a settings read, not a literal — and the remedy the repository
    # uses everywhere else is to not spell it. Argument order is
    # `(backend, <the key>, base_url)`.
    orchestrator = EmbeddingOrchestrator(
        settings.embedding_backend,
        voyage_key,
        settings.voyage_base_url,
    )

    async def embed(text: str) -> Sequence[float]:
        result = await orchestrator.generate_embedding(text)
        return result.vector

    return embed


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


async def _check_opa(http: Any, opa_url: str) -> None:
    """Quick OPA check with a 2s timeout (§11.7, task 9.2).

    Belongs in readiness for the same reason Cerbos does and the IdP does not: with double
    policy evaluation, a replica that cannot reach OPA denies every mutation — the
    chokepoint's stage 1 fails closed — so it is serving refusals for the whole governed
    surface and should be drained. An Authentik outage only degrades login (D-56).

    OPA's own `/health` is used rather than a policy query. A query would also exercise the
    bundle, and that is the wrong thing for a liveness probe to gate on: a replica whose
    bundle is undefined must answer 503 to a *mutation* with `governance-policy-undefined`,
    which is a diagnosable problem document, rather than fall out of the load balancer with
    no explanation anywhere.
    """
    response = await http.get(f"{opa_url.rstrip('/')}/health", timeout=2.0)
    response.raise_for_status()


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

    # --- the ARQ pool the task dispatcher enqueues onto -----------------------
    #
    # Declared here and created ON FIRST USE by `src.policies.routes.get_bundle_service`, not in this
    # lifespan. `.env.example` ships `TASK_DISPATCHER=arq`, and `build_dispatcher` raises
    # "TASK_DISPATCHER=arq requires an ARQ pool; call create_arq_pool first" when handed no pool --
    # nothing called `create_arq_pool` and nothing set this attribute, so every route that enqueues
    # work answered 500 on the committed default configuration. `POST /api/v1/policies/publish` is
    # one, which is why no policy bundle could be published, no device pinned to one, and the
    # governance chokepoint refused every generation submission as "policy bundle stale".
    #
    # Creating it HERE was the first attempt and it was wrong twice over: `arq.create_pool` connects
    # and retries despite `create_arq_pool`'s docstring, so on a host without that Redis the lifespan
    # logged "redis connection error redis:6379" once a second until `ci / secrets` timed out; and
    # bounding it still charged every app construction in the test suite for a connection attempt,
    # which is a cost paid by more than a thousand tests to benefit one route. Lazy creation charges
    # only the request that needs it, and leaves an unreachable Redis as a readiness matter (§4.4).
    app.state.arq_pool = None

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
    # §4.4 keeps the MCP surface's token contract unchanged while giving it a principal,
    # so `require_mcp_principal` verifies with the GATEWAY audience. Exposed under a
    # second name because that dependency must not have to know it is the MCP verifier —
    # if the surfaces ever diverge, only this line changes.
    app.state.token_verifier = mcp_verifier
    # The product API's verifier. Its audience is DISTINCT from the gateway's, so a
    # token minted for one cannot be replayed against the other (§11.2). Two instances
    # of one class rather than one shared instance, for exactly that reason.
    app.state.app_token_verifier = AppTokenVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_app_audience,
        jwks_ttl_seconds=settings.mcp_oidc_jwks_ttl_seconds,
        http=shared_http,
    )
    # ── The login flow (§3.5, §11.2, task 6.2) ───────────────────────────────
    # Three collaborators, and the audiences are why they are three and not one. The
    # ID token is audienced to the CLIENT ID and is a statement to this client about
    # who logged in; the access token above is audienced to the app API. Verifying
    # either with the other's audience would accept a token minted for a different
    # purpose, so the verifiers are separate instances constructed with different
    # audiences and are never shared.
    app.state.id_token_verifier = IdTokenVerifier(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        jwks_ttl_seconds=settings.mcp_oidc_jwks_ttl_seconds,
        http=shared_http,
    )
    app.state.oidc_client = OidcClient(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        redirect_url=settings.oidc_redirect_url,
        http=shared_http,
        # Where a BROWSER can reach the IdP, when that is not where this process reaches it. Empty for
        # a single host; set in the Compose overlay, where the backend uses an internal service name a
        # browser cannot resolve. Only the authorization redirect is rewritten.
        public_base_url=settings.oidc_public_base_url,
    )
    app.state.session_service = SessionService(
        pepper=settings.envelope_pepper.get_secret_value(),
        refresh_ttl_seconds=settings.refresh_ttl_seconds,
    )
    # ── Resource-scoped authorisation (§11.2, task 6.4, D-55) ────────────────
    # Over the SHARED httpx client, which is what §11.1 specifies and what D-55 records
    # as the reason the vendor SDK is not pinned: its constructor owns its own
    # transport, and its published metadata would drag grpcio-tools and protobuf into
    # the runtime image to make one JSON POST. Non-destructive like every other
    # collaborator here — an unreachable Cerbos changes readiness, not liveness.
    app.state.cerbos = CerbosClient(str(settings.cerbos_url), http=shared_http)
    # The audit writer (§11.9). Holds no session and opens no connection — every method takes
    # the caller's `AsyncSession`, which is what lets the audit row and the change-set
    # transition commit or roll back together (Q-04). So there is nothing here to fail at
    # startup and nothing to close at shutdown; it is composed rather than constructed lazily
    # only so §0.4.1's wiring test can see it on `app.state`.
    app.state.audit_writer = AuditWriter(advisory_lock_key=settings.audit_advisory_lock_key)
    # ── The governance chokepoint (§2.2, §11.6, leaf 7.5) ─────────────────────
    # Composed here rather than constructed per request, and composed with its collaborators
    # named explicitly, because §2.2's claim is that the six stages "cannot be skipped and
    # cannot be reordered". A chokepoint assembled at a call site would let one caller assemble
    # it differently, and the assembly is where a stage would go missing.
    #
    # One of the seven collaborators was a deliberately fail-closed placeholder until leaf 9.2,
    # and `DeviceService` took its place.
    from src.secrets.store import InfisicalStore, LocalSealedStore

    if settings.secret_backend == "infisical":
        app.state.secret_store = InfisicalStore(
            http=shared_http,
            base_url=str(settings.infisical_url),
            client_id=settings.infisical_client_id,
            client_secret=settings.infisical_client_secret.get_secret_value(),
        )
    else:
        app.state.secret_store = LocalSealedStore(
            master_key=settings.local_secret_seal_key.get_secret_value().encode("utf-8")
        )
    #
    #   * `UnavailableGovernancePolicy` raised on every evaluation, which the chokepoint turns
    #     into a DENY with an audit record (§11.6: "an OPA outage denies"). **Leaf 9.2 replaces
    #     it with `OpaGovernancePolicy`**, querying the bundle leaf 9.1 authored over the shared
    #     `httpx` client. The placeholder stays in the tree and stays tested: it is what a
    #     deployment with no governance bundle should compose, and `test_wiring_governance.py`
    #     asserts the composed source is now the real one.
    #   * `UnavailableCommandSink` refused delivery with `device-not-connected` until leaf 8.4;
    #     `AgentHub` replaces it below and keeps that refusal for a device with no live session.
    #
    # A permissive default for either would let a mutation through on the strength of nothing
    # objecting, which is precisely what §9's convention forbids.
    app.state.governance_policy = OpaGovernancePolicy(opa_url=str(settings.opa_url), http=shared_http)
    # Read by `/health/ready`. Held on state rather than re-derived from settings there, so
    # readiness reports on the URL the composed client actually queries.
    app.state.opa_url = str(settings.opa_url)
    app.state.envelope_sequencer = RedisEnvelopeSequencer(redis_client)
    # The internal CA (§3.1, §14.2). Constructed only when BOTH PEMs are configured; otherwise the
    # fail-closed stand-in, so a fresh clone starts and every route except the exchange works,
    # while the exchange answers 503 `pairing-unavailable` rather than issuing a certificate no CA
    # vouches for. `make init-ca` populates the two variables into the untracked `.env`.
    #
    # A malformed PEM is a startup failure, deliberately unlike an absent one: absent means "not
    # configured yet", malformed means "configured wrongly", and the second must not degrade
    # silently into the first.
    ca_cert_pem = settings.internal_ca_cert_pem.get_secret_value().strip()
    ca_key_pem = settings.internal_ca_key_pem.get_secret_value().strip()
    device_ca: Any
    if ca_cert_pem and ca_key_pem:
        device_ca = InternalCertificateAuthority(
            cert_pem=ca_cert_pem,
            key_pem=ca_key_pem,
            ttl_hours=settings.device_cert_ttl_hours,
            renew_before_hours=settings.device_cert_renew_before_hours,
        )
    else:
        device_ca = UnavailableCertificateAuthority()
        logger.warning(
            "no internal CA configured; agent pairing will refuse with 503 until `make init-ca` "
            "populates INTERNAL_CA_CERT_PEM and INTERNAL_CA_KEY_PEM",
        )
    app.state.device_ca = device_ca
    app.state.device_service = DeviceService(
        pepper=settings.envelope_pepper.get_secret_value(),
        recorder=GovernanceDeviceAuditRecorder(writer=app.state.audit_writer),
        redis=redis_client,
        limiter=TokenBucketPairingLimiter(
            # §14.6's two exchange buckets, constructed here because `RedisTokenBucketLimiter`
            # lives in `src/ai/**`, which the §2.4 Ruff table bans everywhere except the
            # composition root. The existing limiter is reused rather than a second Lua token
            # bucket written: two of them would be two places for §14.1's "Redis is the single
            # time authority" to be got wrong.
            #
            # Per IP: capacity 10, refilling at 10/60 per second — §14.6's "10 exchange attempts
            # per IP per minute". Global: capacity 600 over one code lifetime, so the refill rate
            # is 600/TTL and "total attempts across the window cannot exceed 600" holds by
            # construction rather than by a comment.
            per_ip=RedisTokenBucketLimiter(
                redis=redis_client,
                capacity=settings.pairing_rate_limit_per_ip_per_minute,
                refill_rate=settings.pairing_rate_limit_per_ip_per_minute / 60.0,
                key_prefix="forgeops:pair:ip:",
            ),
            global_bucket=RedisTokenBucketLimiter(
                redis=redis_client,
                capacity=settings.pairing_rate_limit_global_per_window,
                refill_rate=settings.pairing_rate_limit_global_per_window / settings.pairing_code_ttl_seconds,
                key_prefix="forgeops:pair:global:",
            ),
        ),
        ca=device_ca,
        code_ttl_seconds=settings.pairing_code_ttl_seconds,
        max_attempts=settings.pairing_code_max_attempts,
        alphabet=settings.pairing_code_alphabet,
    )
    # The agent hub (§11.10, leaf 8.4). Constructed AFTER `device_service`, because it
    # authenticates through it, and BEFORE the chokepoint, because it is the chokepoint's sink.
    # `UnavailableCommandSink` is gone from the graph: the hub keeps its refusal — a device with no
    # live session still gets `device-not-connected` — so nothing that used to fail closed now
    # succeeds silently.
    #
    # `TlsPeerCertificate` is the default certificate source and trusts NO header. A deployment
    # that terminates TLS at a proxy composes a source that reads the proxy's verified-client
    # header; until it does, a plaintext connection produces no certificate and the handshake is
    # refused, which is the correct answer for "mTLS is not actually in place".
    agent_hub = AgentHub(
        HubDeps(
            redis=redis_client,
            devices=app.state.device_service,
            sessionmaker=sessionmaker,
            progress=RedisProgressSink(redis_client),
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
            heartbeat_timeout_seconds=settings.heartbeat_timeout_seconds,
        )
    )
    app.state.agent_hub = agent_hub
    app.state.command_sink = agent_hub
    app.state.client_certificate_source = TlsPeerCertificate()
    # Promptness only. The correctness guarantee is the per-message `SISMEMBER` inside the hub, so
    # this task failing is a slower close and never a missed revocation.
    revocation_task = asyncio.create_task(agent_hub.subscribe_revocations(channel=REVOCATION_CHANNEL))

    app.state.governance_chokepoint = GovernanceChokepoint(
        policy=app.state.governance_policy,
        approval_gate=ThresholdApprovalGate(),
        analyzer=SemanticPlanAnalyzer(),
        audit_writer=app.state.audit_writer,
        sequencer=app.state.envelope_sequencer,
        sink=app.state.command_sink,
        envelope_pepper=settings.envelope_pepper.get_secret_value(),
        envelope_max_age_seconds=settings.envelope_max_age_seconds,
    )
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
    # ── criterion 14: L2 similarity, constructed rather than merely implemented ──
    # The L2 tier and its tests predate this wiring by a session; the cache was built
    # here as `TieredSemanticCache(redis=redis_client)`, so L2 was unreachable at runtime
    # and `settings.semantic_cache_threshold` was read by nothing. Finding 79.
    #
    # The threshold is read from settings rather than re-literalled, so the deployment's
    # `SEMANTIC_CACHE_THRESHOLD` is what the cache admits on -- the same D1 argument the
    # tier config above is wired for.
    #
    # Optional, following the `device_ca` shape directly above rather than a new one: a
    # missing embedder is L1-only and logged, never a startup failure. An exact-match
    # cache is a working cache; refusing to boot without an embedding provider would make
    # a paid third-party key a hard dependency of the whole backend.
    cache_embedder = _build_cache_embedder(settings)
    if cache_embedder is None:
        semantic_cache = TieredSemanticCache(redis=redis_client)
        logger.info(
            "semantic cache: L1 exact-match only. L2 similarity is inactive because no "
            "input-sensitive embedding backend is configured (EMBEDDING_BACKEND=%s, "
            "LLM_KEY_VOYAGE unset or placeholder); set a real Voyage key to enable it",
            settings.embedding_backend,
        )
    else:
        semantic_cache = TieredSemanticCache(
            redis=redis_client,
            embed=cache_embedder,
            similarity_threshold=settings.semantic_cache_threshold,
        )
        logger.info(
            "semantic cache: L1 exact-match and L2 similarity active (backend=%s, threshold=%s)",
            settings.embedding_backend,
            settings.semantic_cache_threshold,
        )
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
        # Cancelled before Redis closes: the subscriber holds a connection from the same pool, and
        # tearing the pool down under it produces a shutdown traceback that hides real failures.
        revocation_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await revocation_task
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
    #                -> TenantContext(6)
    #
    # Row 6 is the innermost, and that placement is the point: it runs INSIDE
    # authentication, so it reads an already-verified principal rather than trusting a
    # request header (§4.3, §6.7, D-35). Registered FIRST because Starlette reverses
    # the order.
    app.add_middleware(TenantContextMiddleware)  # executes 6th, innermost
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
    # The auth flow (§3.5). Registered first because it is the only router whose routes
    # are all public: `check-route-auth.py` reads the same `PUBLIC_ROUTES` set, so the
    # four exemptions here are asserted against the real router rather than trusted.
    from .auth.routes import router as auth_router

    app.include_router(auth_router, prefix=settings.api_prefix)

    # Agent pairing and revocation (§3.1, §11.2). TWO routers on one prefix: the protected pair
    # carries router-level `require_principal`, and the exchange has none because §4.4 makes it
    # the one new public route. Splitting them is what keeps the exemption visible in the code
    # that declares the route rather than hidden in a path matcher.
    from .auth.agent_routes import public_router as agent_public_router
    from .auth.agent_routes import router as agent_router

    app.include_router(agent_router)
    app.include_router(agent_public_router)

    # The device READ surface (§3.7, criterion 10 step 4). Pairing was write-only — a POST to mint,
    # a public POST to exchange, a DELETE to revoke, and no GET — so a paired agent could not be
    # observed and the /pairing screen had nothing to read. Its own module so the public exemption
    # above stays visible next to the route that carries it.
    from .auth.device_read_routes import router as device_read_router

    app.include_router(device_read_router)

    # The agent hub (§11.10). One WebSocket route, authenticated inside its handshake by the
    # client certificate and the bearer device token — which is why `check-route-auth.py` reports
    # the path rather than passing it: a WebSocket route has no method set for it to examine.
    from .websocket.routes import router as agent_hub_router

    app.include_router(agent_hub_router)

    from .analysis.routes import router as analysis_router

    app.include_router(analysis_router)

    from .projects.routes import router as projects_router

    app.include_router(projects_router)

    from .ai.routes import router as ai_router

    app.include_router(ai_router)

    # The audit read surface (§11.9, criterion 9). Registered here rather than behind a feature
    # flag: `GET /verify` is what makes tamper evidence a product feature, and a feature nobody
    # can reach is a claim rather than a control.
    from .audit.routes import router as audit_router

    app.include_router(audit_router)

    from .policies.routes import router as policies_router

    app.include_router(policies_router, prefix=settings.api_prefix)

    # The change-approval surface (§3.6, §11.6, criterion 10 steps 8-9). Mounted here for the
    # first time: the router existed since Phase 1's first wave and was deliberately left out of
    # this list, because it required no authentication and took the approver as a query parameter
    # defaulting to `admin`. `check-route-auth.py` would have failed the build, which is why the
    # honest move was to leave it unmounted rather than register it and exempt it.
    #
    # It is registrable now because it carries router-level `require_principal`, derives the
    # approver from the verified principal, and delegates every transition to
    # `GovernanceChokepoint` — so approving through HTTP takes the same six stages, the same
    # optimistic concurrency and the same audit record as approving through any other path. No
    # entry was added to `PUBLIC_ROUTES` for it.
    from .approvals.routes import router as approvals_router

    app.include_router(approvals_router)

    # The generation surface (§1.5, §7.4, §11.5, criterion 10 steps 6-7). `generation/` had twelve
    # modules and no routes.py, so the pipeline and the `generation_runs` table from revision 0008
    # were unreachable over HTTP. One streaming endpoint, because the service exposes one method.
    from .generation.routes import router as generation_router

    app.include_router(generation_router)

    from .secrets.routes import router as secrets_router

    app.include_router(secrets_router)

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

        # Cerbos check (§2.3, task 6.4). Unlike the IdP, an authorisation-sidecar
        # outage DOES belong here: deny-by-default means a request whose permission
        # cannot be evaluated is refused, so a replica that cannot reach Cerbos is
        # serving 503s to every non-public route and should be drained. That is the
        # precise difference from Authentik, which only affects login (§6.3, D-56).
        cerbos = getattr(request.app.state, "cerbos", None)
        if cerbos is not None:
            try:
                await asyncio.wait_for(cerbos.health(), timeout=2.0)
            except TimeoutError:
                errors.append({"dependency": "cerbos", "detail": "health check timed out"})
            except Exception as exc:
                errors.append({"dependency": "cerbos", "detail": str(exc)})

        # OPA (§11.7, task 9.2). Same argument as Cerbos, one layer up: Cerbos decides who
        # may ask, OPA decides whether the governance bundle permits it, and a replica that
        # cannot reach OPA denies every mutation at the chokepoint's stage 1.
        opa_http = getattr(request.app.state, "shared_http", None)
        opa_url = getattr(request.app.state, "opa_url", None)
        if opa_http is not None and opa_url:
            try:
                await asyncio.wait_for(_check_opa(opa_http, opa_url), timeout=2.0)
            except TimeoutError:
                errors.append({"dependency": "opa", "detail": "health check timed out"})
            except Exception as exc:
                errors.append({"dependency": "opa", "detail": str(exc)})

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

        checks = {"postgres": "ok", "redis": "ok"}
        if cerbos is not None:
            checks["cerbos"] = "ok"
        if opa_http is not None and opa_url:
            checks["opa"] = "ok"
        return JSONResponse({"status": "ready", "checks": checks})

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
