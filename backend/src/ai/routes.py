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

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from ..auth.dependencies import require_mcp_principal
from ..core.errors import ProblemException
from ..core.security import TokenVerifier, VerifiedClaims
from .rate_limit.redis_bucket import RateLimitServiceError, RedisTokenBucketLimiter
from .routing.breaker import BreakerState, CircuitBreaker
from .routing.endpoints import CompletionRequest, EndpointRegistry
from .routing.router import ModelRouter, RoutingResult
from .routing.tiers import ModelTier, TierConfig

router = APIRouter(
    prefix="/api/v1/ai",
    tags=["ai"],
    # §4.4 names `/api/v1/ai/complete` alongside the MCP surface: same token contract,
    # so the same gateway-audience dependency. `/tiers` is under the same router and
    # therefore protected too, which is correct — the tier map names every configured
    # endpoint and is not information an unauthenticated caller needs.
    dependencies=[Depends(require_mcp_principal)],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class TierInfoResponse(BaseModel):
    name: str
    primary_endpoint: str
    primary_protocol: str
    available: bool
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


@router.get("/tiers", response_model=TiersListResponse)
async def list_tiers(deps: AIDeps = Depends(_get_ai_deps)) -> TiersListResponse:
    """Return all tier names with primary endpoint info, availability, and breaker state."""
    tiers_info: list[TierInfoResponse] = []

    for tier, chain in deps.tier_config.tiers.items():
        primary_id = chain.primary
        descriptor = deps.tier_config.endpoints.get(primary_id)
        availability = deps.registry.get_availability(primary_id)
        breaker = deps.breakers.get(primary_id)

        tiers_info.append(
            TierInfoResponse(
                name=tier.value,
                primary_endpoint=primary_id,
                primary_protocol=descriptor.protocol.value if descriptor else "unknown",
                available=availability.available if availability else False,
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

    from src.generation.context import assemble_prompt
    from src.secrets.redaction import create_redacted_chunk, create_redacted_instruction

    redacted_prompt = assemble_prompt(
        system=create_redacted_chunk(""), chunks=[], instruction=create_redacted_instruction(body.prompt)
    )

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
