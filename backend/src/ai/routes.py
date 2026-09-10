# SPDX-License-Identifier: FSL-1.1-ALv2
"""AI model routing HTTP endpoints (Design §13.8).

GET  /api/v1/ai/tiers    — tier names, protocols, availability, breaker state.
POST /api/v1/ai/complete  — OIDC verify → limiter → cache → router/provider.

Security/admission order for /complete:
  1. OIDC verify (401 on failure)
  2. Require claims.sub
  3. Redis rate limiter (503 on Redis failure, 429 on exhaustion)
  4. Semantic cache check
  5. Registry/router/provider cascade
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response

from ..auth.dependencies import require_mcp_principal, require_principal, require_role
from ..auth.models import UserRole
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import ProblemException
from ..core.security import TokenVerifier, VerifiedClaims
from .custom_endpoints import load_custom_endpoints
from .endpoint_probe import probe_endpoint
from .provider_credentials import derive_seal_key, hint_for, seal, unseal
from .provider_models import SUPPORTED_CUSTOM_PROTOCOL, ProviderCredential
from .rate_limit.redis_bucket import RateLimitServiceError, RedisTokenBucketLimiter
from .routing.breaker import BreakerState, CircuitBreaker
from .routing.endpoints import CompletionRequest, EndpointRegistry, _availability_reason
from .routing.router import ModelRouter, RoutingResult
from .routing.tiers import ModelTier, TierConfig

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/ai",
    tags=["ai"],
    # §4.4 names `/api/v1/ai/complete` alongside the MCP surface: same token contract, so the same
    # gateway-audience dependency. THIS ROUTER IS NOW THE COMPLETION SURFACE ONLY, and its auth is
    # deliberately unchanged — the fix below moves a read off it rather than widening it.
    dependencies=[Depends(require_mcp_principal)],
)

# A SECOND ROUTER, for the read a HUMAN performs rather than a gateway.
#
# `GET /tiers` used to sit on the router above and inherit `require_mcp_principal`, which demands the
# GATEWAY AUDIENCE. A browser session token cannot satisfy that, so the Models screen showed
#
#     Not authenticated to read model tiers.
#
# to a correctly signed-in user, for ever, no matter how many times they signed in again. The old
# comment argued the tier map "is not information an unauthenticated caller needs" — true, and beside
# the point: the caller here is AUTHENTICATED, just not as a machine. The dependency was answering a
# question nobody asked.
#
# Same prefix and tag, so the API surface is unchanged from a caller's point of view. Separate router
# because FastAPI applies a router-level dependency to EVERY route on it, so the only way to give one
# route a different principal contract is to give it a different router. Deny-by-default is preserved:
# this router still requires a principal, it simply requires a USER one.
read_router = APIRouter(
    prefix="/api/v1/ai",
    tags=["ai"],
    dependencies=[Depends(require_principal)],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class TierInfoResponse(BaseModel):
    """What is known about a tier — and, as importantly, what is not.

    `available` USED TO MEAN "an adapter exists for this protocol" and was rendered as "this will answer". A
    fresh install therefore reported three tiers as available with nothing behind them but the
    placeholders `.env.example` ships.
    The facts are separated here so the screen can state each one and stop implying the others.
    """

    name: str
    primary_endpoint: str
    primary_protocol: str
    #: `protocol_supported and credential_configured` — "nothing is known to be missing".
    #:
    #: STILL NOT A PROMISE THAT IT WORKS. A configured key can be expired, revoked, or a placeholder; only a
    #: real call settles that, which is what `last_test_ok` reports.
    available: bool
    #: Static, about ForgeOps: an adapter exists for this protocol. No configuration changes it.
    protocol_supported: bool
    #: About this deployment, and fixable by the operator.
    credential_configured: bool
    #: False for a local server, where "no credential" is correct rather than missing.
    credential_required: bool
    #: WHICH credential this tier's primary needs, so the screen can offer the right box to fill.
    #:
    #: Named explicitly rather than left to the client, which had to recover it by matching a quoted
    #: substring out of `reason` — a prose string, whose wording is free to change without notice. A
    #: field the client needs in order to act belongs in the schema.
    #:
    #: Null when nothing needs one. Providers are shared: several tiers can name the same `key_ref`, and
    #: setting it once configures all of them.
    key_ref: str | None = None
    #: The specific cause when something is missing, distinguishing a gap in ForgeOps from a gap in setup.
    reason: str | None = None
    #: The outcome of the last real call made with this endpoint's credential. None means NEVER TESTED,
    #: which is not the same as failing and must not be rendered as it.
    last_test_ok: bool | None = None
    last_tested_at: str | None = None
    last_test_detail: str = ""
    breaker_state: str


class TiersListResponse(BaseModel):
    tiers: list[TierInfoResponse]


class CompletionRequestBody(BaseModel):
    tier: str = Field(..., description="Model tier name (e.g. 'high_coding')")
    prompt: str = Field(..., min_length=1, description="The prompt to complete")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=128_000)


class CompletionResponseBody(BaseModel):
    content: str | None = None
    endpoint_id: str | None = None
    served_from: str | None = None
    degraded: bool = False
    outcome: str


# ---------------------------------------------------------------------------
# Dependency protocols for testability
# ---------------------------------------------------------------------------


class AIDeps:
    """Container for AI route dependencies — injectable for testing."""

    def __init__(
        self,
        *,
        tier_config: TierConfig,
        registry: EndpointRegistry,
        breakers: dict[str, CircuitBreaker],
        model_router: ModelRouter,
        limiter: RedisTokenBucketLimiter,
        verifier: TokenVerifier,
    ) -> None:
        self.tier_config = tier_config
        self.registry = registry
        self.breakers = breakers
        self.model_router = model_router
        self.limiter = limiter
        self.verifier = verifier


def _get_ai_deps(request: Request) -> AIDeps:
    """Retrieve AI dependencies from app state."""
    return request.app.state.ai_deps


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def load_provider_credentials(
    session: AsyncSession, *, seal_key: bytes
) -> dict[str, tuple[str, ProviderCredential]]:
    """Every stored credential, unsealed, keyed by `key_ref`.

    Returns the value AND the row, because the caller needs both the secret (to resolve) and the metadata
    (to report when it was last tested). The secret never leaves the process: `ProviderCredentialResponse`
    carries a length and a hint, and there is no route that returns a value.

    A row that fails to decrypt is SKIPPED rather than raising. That happens when `LOCAL_SECRET_SEAL_KEY` has
    changed, and the honest consequence is that the credential is no longer configured — which is what the
    screen will then say, with a reason. Raising would take the whole Models page down for one bad row.
    """
    rows = (await session.execute(select(ProviderCredential))).scalars().all()
    out: dict[str, tuple[str, ProviderCredential]] = {}
    for row in rows:
        try:
            out[row.key_ref] = (unseal(row.encrypted_value, seal_key), row)
        except Exception:  # noqa: BLE001 — any decryption failure means the same thing to the caller
            continue
    return out


def _seal_key(request: Request) -> bytes:
    """The 32-byte key credentials are sealed under, from settings."""
    settings = request.app.state.settings
    # DERIVED, not used raw. `LOCAL_SECRET_SEAL_KEY` is whatever length the operator configured — 17
    # characters in the deployments this runs in — and AES-256-GCM needs exactly 32 bytes.
    return derive_seal_key(settings.local_secret_seal_key.get_secret_value())


class ProviderCredentialResponse(BaseModel):
    """What is known about a stored credential, and never the credential.

    No `value` field, deliberately. §7.11 keeps secret values out of anything that leaves the process, and a
    reveal affordance is the one that ends up in a screenshot. `length` and `hint` are enough to show that
    something is configured and to tell two keys for one provider apart.
    """

    key_ref: str
    length: int
    hint: str
    last_tested_at: str | None = None
    last_test_ok: bool | None = None
    last_test_detail: str = ""


class ProviderCredentialRequest(BaseModel):
    """A credential an operator is setting.

    Trimmed, because a key pasted from a web page routinely carries whitespace and a leading or trailing
    space produces a 401 that looks exactly like a wrong key.
    """

    value: str = Field(min_length=1, max_length=1024)

    @field_validator("value")
    @classmethod
    def _trim(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("the credential is empty once surrounding whitespace is removed")
        return trimmed


@read_router.get("/credentials", response_model=list[ProviderCredentialResponse])
async def list_provider_credentials(
    request: Request,
    _principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ProviderCredentialResponse]:
    """Which providers have a credential configured, and what the last real call said about each."""
    stored = await load_provider_credentials(session, seal_key=_seal_key(request))
    return [
        ProviderCredentialResponse(
            key_ref=ref,
            length=record.value_length,
            hint=record.value_hint,
            last_tested_at=record.last_tested_at.isoformat() if record.last_tested_at else None,
            last_test_ok=record.last_test_ok,
            last_test_detail=record.last_test_detail,
        )
        for ref, (_value, record) in sorted(stored.items())
    ]


class ConnectionTestResponse(BaseModel):
    """What happened when a real request was sent.

    `ok` IS EVIDENCE, NOT CONFIGURATION. The whole defect this feature answers is a screen that reported
    "available" from a static fact; a test that only checked the key was non-empty would repeat it one layer
    down. So this sends an actual completion and reports the status line the provider returned.
    """

    endpoint_id: str
    ok: bool
    #: The provider's own words: an HTTP status, or the transport error. Never paraphrased — an operator
    #: needs to tell "wrong key" (401) from "wrong model name" (404) from "cannot reach the host".
    detail: str
    #: The model the endpoint echoed back, when it answered. A provider that accepts the request but serves a
    #: different model is a real misconfiguration and invisible from the status code alone.
    model_reported: str | None = None
    latency_ms: int = 0


@read_router.put("/credentials/{key_ref}", response_model=ProviderCredentialResponse)
async def set_provider_credential(
    key_ref: str,
    body: ProviderCredentialRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_role(UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProviderCredentialResponse:
    """Store a provider credential, sealed, and refresh the resolver so it takes effect at once.

    ADMIN ONLY. A provider key spends the deployment's money and reaches an external service, which is not a
    developer-level decision — and unlike most settings here, a wrong value is billed rather than refused.

    UPSERT ON `key_ref`, so rotating a key is the same operation as setting one. The previous value is
    overwritten rather than versioned: this is a pointer at a live credential, not an audit trail, and
    keeping superseded secrets is a liability with no reader.

    The test metadata is CLEARED on write, because it described the old value. Leaving `last_test_ok = true`
    beside a freshly-pasted key is exactly the stale-evidence problem this whole change is about.
    """
    ref = key_ref.strip().lower()
    if not ref or not ref.replace("-", "").replace("_", "").isalnum():
        raise ProblemException(
            status=422,
            type_suffix="invalid-key-ref",
            title="Invalid provider reference",
            detail=(
                "A provider reference is the name the tier configuration uses — for example 'openai' or "
                "'anthropic' — and may contain only letters, digits, hyphens and underscores."
            ),
        )

    sealed = seal(body.value, _seal_key(request))
    await session.execute(
        text(
            "INSERT INTO provider_credentials "
            "(key_ref, encrypted_value, value_length, value_hint, updated_by, created_at, updated_at) "
            "VALUES (:ref, :sealed, :length, :hint, :actor, now(), now()) "
            "ON CONFLICT (key_ref) DO UPDATE SET "
            "encrypted_value = EXCLUDED.encrypted_value, value_length = EXCLUDED.value_length, "
            "value_hint = EXCLUDED.value_hint, updated_by = EXCLUDED.updated_by, updated_at = now(), "
            # Cleared, because they described the value being replaced.
            "last_tested_at = NULL, last_test_ok = NULL, last_test_detail = ''"
        ),
        {
            "ref": ref,
            "sealed": sealed,
            "length": len(body.value),
            "hint": hint_for(body.value),
            "actor": principal.user_id if principal.kind == "user" else None,
        },
    )
    await session.commit()

    # Refreshed immediately: an operator who sets a key expects the screen to change without a restart.
    stored = await load_provider_credentials(session, seal_key=_seal_key(request))
    resolver = getattr(request.app.state, "key_resolver", None)
    if resolver is not None:
        resolver.replace_snapshot({r: v for r, (v, _record) in stored.items()})

    record = stored[ref][1]
    return ProviderCredentialResponse(key_ref=ref, length=record.value_length, hint=record.value_hint)


@read_router.delete("/credentials/{key_ref}", status_code=204)
async def delete_provider_credential(
    key_ref: str,
    request: Request,
    _principal: Annotated[Principal, Depends(require_role(UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Remove a stored credential, falling back to the environment if one is set there.

    Not an error when absent: the caller's intent is "there should be no credential here", and that is
    already true. Answering 404 would make a retry after a partial failure look like a new problem.
    """
    await session.execute(
        text("DELETE FROM provider_credentials WHERE key_ref = :ref"), {"ref": key_ref.strip().lower()}
    )
    await session.commit()
    stored = await load_provider_credentials(session, seal_key=_seal_key(request))
    resolver = getattr(request.app.state, "key_resolver", None)
    if resolver is not None:
        resolver.replace_snapshot({r: v for r, (v, _record) in stored.items()})
    return Response(status_code=204)


@read_router.post("/endpoints/{endpoint_id}/test", response_model=ConnectionTestResponse)
async def test_endpoint_connection(
    endpoint_id: str,
    request: Request,
    _principal: Annotated[Principal, Depends(require_role(UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_session)],
    deps: AIDeps = Depends(_get_ai_deps),
) -> ConnectionTestResponse:
    """Send a real completion to one endpoint and report exactly what came back.

    A REAL CALL, deliberately. Checking that a key is non-empty would reproduce the original defect one layer
    down: the Models screen reported "available" from a static fact, and a test that inspected configuration
    rather than behaviour would be equally confident and equally uninformed. Only a request establishes that
    a key is accepted, that the model name exists, and that the host is reachable.

    THE PROVIDER'S OWN WORDS ARE RETURNED. 401, 404 and a DNS failure have three different remedies, and an
    operator cannot tell them apart from "test failed".

    The outcome is recorded against the credential so the Models screen can show evidence rather than
    presence — and `None` there keeps meaning "never tested", which is not the same as failing.
    """
    descriptor = deps.tier_config.endpoints.get(endpoint_id)
    if descriptor is None:
        raise ProblemException(
            status=404,
            type_suffix="unknown-endpoint",
            title="Unknown endpoint",
            detail=f"No endpoint named '{endpoint_id}' is configured.",
        )
    if descriptor.protocol.value != "openai_compatible":
        raise ProblemException(
            status=422,
            type_suffix="unsupported-protocol",
            title="No adapter for this protocol",
            detail=(
                f"'{endpoint_id}' speaks {descriptor.protocol.value}, and ForgeOps has no client for it. "
                "This is a gap in ForgeOps rather than in your configuration, and no credential will "
                "change it."
            ),
        )

    stored = await load_provider_credentials(session, seal_key=_seal_key(request))
    resolver = getattr(request.app.state, "key_resolver", None)
    if resolver is not None:
        resolver.replace_snapshot({r: v for r, (v, _record) in stored.items()})

    credential: str | None = None
    if descriptor.key_ref:
        secret = resolver.resolve(descriptor.key_ref) if resolver else None
        if secret is None:
            raise ProblemException(
                status=422,
                type_suffix="no-credential",
                title="No credential configured",
                detail=(
                    f"'{endpoint_id}' needs a credential for '{descriptor.key_ref}' and none is "
                    "configured. Set one and test again."
                ),
            )
        credential = secret.get_secret_value()

    result = await probe_endpoint(
        base_url=descriptor.base_url,
        model=descriptor.model,
        credential=credential,
        timeout_seconds=request.app.state.settings.model_http_timeout_seconds,
    )

    if descriptor.key_ref and descriptor.key_ref in stored:
        await session.execute(
            text(
                "UPDATE provider_credentials SET last_tested_at = now(), last_test_ok = :ok, "
                "last_test_detail = :detail WHERE key_ref = :ref"
            ),
            {"ok": result.ok, "detail": result.detail[:1024], "ref": descriptor.key_ref},
        )
        await session.commit()

    return ConnectionTestResponse(
        endpoint_id=endpoint_id,
        ok=result.ok,
        detail=result.detail,
        model_reported=result.model_reported,
        latency_ms=result.latency_ms,
    )


def normalise_endpoint_id(value: str) -> str:
    """The canonical form of an operator-chosen endpoint id, or a refusal.

    A MODULE FUNCTION RATHER THAN A FIELD VALIDATOR, because the id arrives in the PATH and not in the body.
    It used to be required in BOTH, and the route refused a mismatch — a failure mode that can only ever be a
    client bug, and one the API invited by asking the same question twice. The path names the resource; the
    body describes it.
    """
    slug = value.strip().lower()
    if not slug or len(slug) > 64 or not slug.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ProblemException(
            status=422,
            type_suffix="invalid-endpoint-id",
            title="That endpoint id is not usable",
            detail=("An endpoint id may be up to 64 characters of letters, digits, hyphens, underscores and dots."),
        )
    return slug


class CustomEndpointRequest(BaseModel):
    """An endpoint an operator is defining.

    ONLY `openai_compatible` IS ACCEPTED, because it is the only protocol with a client. Accepting a
    protocol name we cannot speak would let an operator configure something that can never work and then
    wonder why — which is the defect this whole area is correcting, in a new place.

    NO `id` FIELD: it comes from the path. See `normalise_endpoint_id`.
    """

    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    tier: str = Field(min_length=1, max_length=32)
    #: Optional: a local server needs no key, which is what the self-hosted tier already relies on.
    key_ref: str | None = Field(default=None, max_length=64)

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("the base URL must start with http:// or https://")
        # The probe appends `/chat/completions`, so a base URL that already ends in it would produce a
        # doubled path and a 404 that looks like a wrong model name.
        if url.endswith("/chat/completions"):
            raise ValueError("give the base URL only — the part ending in /v1 — not the /chat/completions path")
        return url


class CustomEndpointResponse(BaseModel):
    id: str
    model: str
    base_url: str
    tier: str
    key_ref: str | None = None
    protocol: str = SUPPORTED_CUSTOM_PROTOCOL


@read_router.get("/endpoints/custom", response_model=list[CustomEndpointResponse])
async def list_custom_endpoints(
    _principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CustomEndpointResponse]:
    """Every endpoint an operator defined."""
    rows = await load_custom_endpoints(session)
    return [
        CustomEndpointResponse(
            id=r.id, model=r.model, base_url=r.base_url, tier=r.tier, key_ref=r.key_ref, protocol=r.protocol
        )
        for r in rows
    ]


@read_router.put("/endpoints/custom/{endpoint_id}", response_model=CustomEndpointResponse)
async def upsert_custom_endpoint(
    endpoint_id: str,
    body: CustomEndpointRequest,
    principal: Annotated[Principal, Depends(require_role(UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CustomEndpointResponse:
    """Define or update an endpoint, and place it in a tier's cascade.

    ADMIN ONLY, and for a sharper reason than most settings: this names a host that generated code and
    repository context will be sent to. That is a data-egress decision.

    THE TIER MUST EXIST. An endpoint naming a tier nothing routes to would be stored, listed, and never
    reached — configured and inert, which is the shape of defect this area is being corrected for.

    A RESTART IS REQUIRED before it serves traffic, and the response says so rather than implying
    otherwise. The tier config, the breakers and the registry are composed during the lifespan; rebuilding
    them under live traffic would mean swapping the cascade mid-request, and a stale breaker is worse than
    a deferred endpoint.
    """
    resolved_id = normalise_endpoint_id(endpoint_id)
    try:
        ModelTier(body.tier)
    except ValueError as exc:
        raise ProblemException(
            status=422,
            type_suffix="unknown-tier",
            title="Unknown tier",
            detail=(
                f"'{body.tier}' is not a tier this deployment routes to. Valid tiers are: "
                + ", ".join(sorted(t.value for t in ModelTier))
            ),
        ) from exc

    await session.execute(
        text(
            "INSERT INTO provider_endpoints (id, model, base_url, protocol, key_ref, tier, updated_by, created_at) "
            "VALUES (:id, :model, :base_url, :protocol, :key_ref, :tier, :actor, now()) "
            "ON CONFLICT (id) DO UPDATE SET model = EXCLUDED.model, base_url = EXCLUDED.base_url, "
            "key_ref = EXCLUDED.key_ref, tier = EXCLUDED.tier, updated_by = EXCLUDED.updated_by"
        ),
        {
            "id": resolved_id,
            "model": body.model,
            "base_url": body.base_url,
            "protocol": SUPPORTED_CUSTOM_PROTOCOL,
            "key_ref": (body.key_ref or "").strip().lower() or None,
            "tier": body.tier,
            "actor": principal.user_id if principal.kind == "user" else None,
        },
    )
    await session.commit()
    return CustomEndpointResponse(
        id=resolved_id, model=body.model, base_url=body.base_url, tier=body.tier, key_ref=body.key_ref
    )


@read_router.delete("/endpoints/custom/{endpoint_id}", status_code=204)
async def delete_custom_endpoint(
    endpoint_id: str,
    _principal: Annotated[Principal, Depends(require_role(UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Remove an operator-defined endpoint. Not an error when absent."""
    await session.execute(text("DELETE FROM provider_endpoints WHERE id = :id"), {"id": endpoint_id.strip().lower()})
    await session.commit()
    return Response(status_code=204)


class ConnectionTestRequest(BaseModel):
    """An endpoint to probe that may not be registered yet.

    THE POINT OF TESTING BEFORE SAVING. An operator pasting a base URL and a key wants to know they are
    right before committing them, and a form that only tests what it has already stored makes them save a
    guess first.
    """

    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    #: Sent directly for an unsaved endpoint. Omit to use the stored credential named by `key_ref`.
    credential: str | None = Field(default=None, max_length=1024)
    key_ref: str | None = Field(default=None, max_length=64)


@read_router.post("/endpoints/probe", response_model=ConnectionTestResponse)
async def probe_arbitrary_endpoint(
    body: ConnectionTestRequest,
    request: Request,
    _principal: Annotated[Principal, Depends(require_role(UserRole.ADMIN))],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionTestResponse:
    """Send a real completion to a base URL and model the caller supplies.

    Lets the Models screen test a custom endpoint BEFORE it is saved, so an operator finds a typo in a base
    URL while the form is still open rather than after committing it.

    A credential supplied here is used and NOT stored. Storing it as a side effect of a test would mean a
    failed test left a bad key configured.
    """
    credential = body.credential
    if credential is None and body.key_ref:
        stored = await load_provider_credentials(session, seal_key=_seal_key(request))
        entry = stored.get(body.key_ref.strip().lower())
        credential = entry[0] if entry else None

    result = await probe_endpoint(
        base_url=body.base_url.strip().rstrip("/"),
        model=body.model.strip(),
        credential=credential,
        timeout_seconds=request.app.state.settings.model_http_timeout_seconds,
    )
    return ConnectionTestResponse(
        endpoint_id=body.model.strip(),
        ok=result.ok,
        detail=result.detail,
        model_reported=result.model_reported,
        latency_ms=result.latency_ms,
    )


@read_router.get("/tiers", response_model=TiersListResponse)
async def list_tiers(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    deps: AIDeps = Depends(_get_ai_deps),
) -> TiersListResponse:
    """Return each tier's endpoint, what is known about it, and what the last real call said.

    THE CREDENTIAL HALF IS COMPUTED LIVE, not read from the registry's start-up snapshot. An operator who
    sets a key expects the screen to reflect it without a restart, and the registry was built once during
    the lifespan. So the snapshot is refreshed from the database here — this is one of the two moments it
    can have changed, the other being a write.

    A DATABASE THAT WILL NOT ANSWER DOES NOT TAKE THIS SCREEN DOWN. This is a diagnostic surface: it reports
    which endpoint each tier would reach and whether its breaker is letting anything through, which are
    exactly the facts somebody wants while something is broken. Answering 500 because the credential table
    could not be read would remove the page precisely when it is needed, so the refresh is best-effort and
    the resolver keeps whatever snapshot it already had. That is the same "last observation" framing the rest
    of the page already states, rather than a new and quieter one.
    """
    stored: dict[str, tuple[str, Any]] = {}
    resolver = getattr(request.app.state, "key_resolver", None)
    try:
        stored = await load_provider_credentials(session, seal_key=_seal_key(request))
        if resolver is not None:
            resolver.replace_snapshot({ref: value for ref, (value, _record) in stored.items()})
    except Exception:  # noqa: BLE001 — every failure to read means the same thing: report what is known
        logger.warning("ai: could not refresh provider credentials; reporting the last known state")

    tiers_info: list[TierInfoResponse] = []

    for tier, chain in deps.tier_config.tiers.items():
        primary_id = chain.primary
        descriptor = deps.tier_config.endpoints.get(primary_id)
        breaker = deps.breakers.get(primary_id)

        protocol_supported = descriptor is not None and descriptor.protocol.value == "openai_compatible"
        key_ref = descriptor.key_ref if descriptor else None
        credential_required = bool(key_ref)
        credential_configured = True
        if credential_required:
            credential_configured = resolver is not None and resolver.resolve(key_ref) is not None

        record = stored.get(key_ref)[1] if key_ref and key_ref in stored else None

        tiers_info.append(
            TierInfoResponse(
                name=tier.value,
                primary_endpoint=primary_id,
                primary_protocol=descriptor.protocol.value if descriptor else "unknown",
                available=protocol_supported and credential_configured,
                protocol_supported=protocol_supported,
                credential_configured=credential_configured,
                credential_required=credential_required,
                key_ref=key_ref,
                reason=_availability_reason(
                    protocol_supported=protocol_supported,
                    credential_configured=credential_configured,
                    key_ref=key_ref,
                    protocol=descriptor.protocol.value if descriptor else "unknown",
                ),
                last_test_ok=record.last_test_ok if record else None,
                last_tested_at=record.last_tested_at.isoformat() if record and record.last_tested_at else None,
                last_test_detail=record.last_test_detail if record else "",
                breaker_state=breaker.state().value if breaker else BreakerState.CLOSED.value,
            )
        )

    return TiersListResponse(tiers=tiers_info)


@router.post("/complete", response_model=CompletionResponseBody)
async def complete(
    body: CompletionRequestBody,
    authorization: str | None = Header(default=None),
    deps: AIDeps = Depends(_get_ai_deps),
) -> Any:
    """Run a completion with the fixed security/admission order.

    Order: OIDC → claims.sub → Redis limiter → cache → router/provider.
    """
    # 1. OIDC verify (401 on failure)
    claims: VerifiedClaims = await deps.verifier.verify(authorization)

    # 2. Require claims.sub
    if not claims.sub:
        raise ProblemException(
            status=401,
            type_suffix="ai-missing-subject",
            title="Missing subject claim",
            detail="Token must contain a 'sub' claim.",
        )

    # 3. Validate tier exists
    try:
        tier = ModelTier(body.tier)
    except ValueError as exc:
        raise ProblemException(
            status=422,
            type_suffix="ai-unknown-tier",
            title="Unknown model tier",
            detail=f"Tier '{body.tier}' is not a valid tier name.",
        ) from exc

    if tier not in deps.tier_config.tiers:
        raise ProblemException(
            status=422,
            type_suffix="ai-unknown-tier",
            title="Unknown model tier",
            detail=f"Tier '{body.tier}' is not configured.",
        )

    # 4. Redis rate limiter (fail-closed → 503, exhausted → 429)
    try:
        decision = await deps.limiter.check(claims.sub)
    except RateLimitServiceError as exc:
        raise ProblemException(
            status=503,
            type_suffix="ai-rate-limit-unavailable",
            title="Rate limit service unavailable",
            detail="Rate limiting backend is unreachable (fail-closed).",
        ) from exc

    if not decision.allowed:
        retry_after = decision.retry_after_seconds or 1.0
        return JSONResponse(
            status_code=429,
            content={
                "type": "https://errors.forgeops.dev/ai-rate-limited",
                "title": "Rate limit exceeded",
                "status": 429,
                "detail": "Token bucket exhausted. Retry later.",
            },
            headers={"Retry-After": str(int(retry_after))},
        )

    from src.secrets.redaction import create_redacted_prompt

    redacted_prompt = create_redacted_prompt(body.prompt)

    # 5. Route through the model router (cache → cascade → provider)
    request = CompletionRequest(
        model=body.tier,  # router uses tier as model selector
        messages=[{"role": "user", "content": redacted_prompt}],
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )

    result: RoutingResult = await deps.model_router.complete(tier=tier, request=request, prompt=redacted_prompt)

    return CompletionResponseBody(
        content=result.content,
        endpoint_id=result.endpoint_id,
        served_from=result.served_from,
        degraded=result.degraded,
        outcome=result.outcome.value,
    )
