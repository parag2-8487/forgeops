# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for AI model routing HTTP endpoints (Task 13.8).

Tests:
- GET /api/v1/ai/tiers returns 6 tiers with endpoint info
- POST /api/v1/ai/complete requires auth (401 without token)
- POST /api/v1/ai/complete with valid flow returns routing result
- Rate limiter deny returns 429
- Rate limiter unavailable returns 503
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.ai.rate_limit.redis_bucket import (
    RateLimitDecision,
    RateLimitServiceError,
    RedisTokenBucketLimiter,
)
from src.ai.routes import AIDeps, router
from src.ai.routing.breaker import CircuitBreaker
from src.ai.routing.endpoints import (
    EndpointAvailability,
    EndpointRegistry,
)
from src.ai.routing.router import ModelRouter, RoutingOutcome, RoutingResult
from src.ai.routing.tiers import (
    EndpointDescriptor,
    EndpointProtocol,
    ModelTier,
    TierChain,
    TierConfig,
)
from src.mcp.auth import OidcTokenVerifier

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rsa_keypair():
    """Generate an RSA key pair for signing test JWTs."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    return private_pem, public_key


@pytest.fixture()
def tier_config() -> TierConfig:
    """Build a tier config with 6 tiers for testing."""
    endpoints = {
        "gpt-5.6-sol": EndpointDescriptor(
            id="gpt-5.6-sol",
            provider="openai",
            model="gpt-5.6-sol",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="https://api.openai.com",
            key_ref="openai",
        ),
        "claude-fable-5": EndpointDescriptor(
            id="claude-fable-5",
            provider="anthropic",
            model="claude-fable-5",
            protocol=EndpointProtocol.ANTHROPIC_NATIVE,
            base_url="https://api.anthropic.com",
            key_ref="anthropic",
        ),
        "grok-4.5": EndpointDescriptor(
            id="grok-4.5",
            provider="xai",
            model="grok-4.5",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="https://api.x.ai",
            key_ref="xai",
        ),
        "deepseek-v4": EndpointDescriptor(
            id="deepseek-v4",
            provider="deepseek",
            model="deepseek-v4",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="https://api.deepseek.com",
            key_ref="deepseek",
        ),
        "gemini-3-flash": EndpointDescriptor(
            id="gemini-3-flash",
            provider="google",
            model="gemini-3-flash",
            protocol=EndpointProtocol.GOOGLE_NATIVE,
            base_url="https://generativelanguage.googleapis.com",
            key_ref="google",
        ),
        "qwen3-coder-next": EndpointDescriptor(
            id="qwen3-coder-next",
            provider="self_hosted",
            model="qwen3-coder-next",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="http://localhost:8080",
            key_ref=None,
        ),
        "glm-5.2": EndpointDescriptor(
            id="glm-5.2",
            provider="self_hosted",
            model="glm-5.2",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="http://localhost:8080",
            key_ref=None,
        ),
        "claude-sonnet-5": EndpointDescriptor(
            id="claude-sonnet-5",
            provider="anthropic",
            model="claude-sonnet-5",
            protocol=EndpointProtocol.ANTHROPIC_NATIVE,
            base_url="https://api.anthropic.com",
            key_ref="anthropic",
        ),
        "deepseek-v4-flash": EndpointDescriptor(
            id="deepseek-v4-flash",
            provider="self_hosted",
            model="deepseek-v4-flash",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="http://localhost:8080",
            key_ref=None,
        ),
    }

    tiers = {
        ModelTier.HIGH_CODING: TierChain(primary="gpt-5.6-sol", secondary="claude-fable-5"),
        ModelTier.HIGH_ANALYSIS: TierChain(primary="claude-fable-5", secondary="gpt-5.6-sol"),
        ModelTier.MEDIUM: TierChain(primary="grok-4.5", secondary="claude-sonnet-5"),
        ModelTier.MEDIUM_VALUE: TierChain(primary="claude-sonnet-5", secondary="deepseek-v4"),
        ModelTier.LOW_LOGS: TierChain(primary="gemini-3-flash", secondary="deepseek-v4"),
        ModelTier.SELF_HOSTED: TierChain(primary="qwen3-coder-next", secondary="glm-5.2"),
    }

    return TierConfig(tiers=tiers, endpoints=endpoints)


@pytest.fixture()
def mock_registry(tier_config: TierConfig) -> EndpointRegistry:
    """Create an endpoint registry with availability info."""
    availability = {}
    for eid, desc in tier_config.endpoints.items():
        if desc.protocol == EndpointProtocol.OPENAI_COMPATIBLE:
            availability[eid] = EndpointAvailability(endpoint_id=eid, available=True)
        else:
            availability[eid] = EndpointAvailability(
                endpoint_id=eid, available=False, reason="unsupported_protocol_phase_0"
            )
    return EndpointRegistry(endpoints={}, availability=availability)


@pytest.fixture()
def mock_breakers() -> dict[str, CircuitBreaker]:
    """Breakers for each endpoint — all closed."""
    return {
        eid: CircuitBreaker()
        for eid in [
            "gpt-5.6-sol",
            "claude-fable-5",
            "grok-4.5",
            "deepseek-v4",
            "gemini-3-flash",
            "qwen3-coder-next",
            "glm-5.2",
            "claude-sonnet-5",
            "deepseek-v4-flash",
        ]
    }


@pytest.fixture()
def mock_model_router() -> AsyncMock:
    """Mock model router that returns a successful result."""
    router_mock = AsyncMock(spec=ModelRouter)
    router_mock.complete = AsyncMock(
        return_value=RoutingResult(
            outcome=RoutingOutcome.OK,
            endpoint_id="gpt-5.6-sol",
            content="Hello, I can help with that!",
            served_from="endpoint",
            degraded=False,
        )
    )
    return router_mock


@pytest.fixture()
def mock_limiter() -> AsyncMock:
    """Mock rate limiter — allows by default."""
    limiter = AsyncMock(spec=RedisTokenBucketLimiter)
    limiter.check = AsyncMock(return_value=RateLimitDecision(allowed=True, remaining=19))
    return limiter


@pytest.fixture()
def mock_verifier(rsa_keypair) -> OidcTokenVerifier:
    """OIDC verifier with mocked JWKS."""
    _, public_key = rsa_keypair
    verifier = OidcTokenVerifier(
        allowed_issuers=["https://auth.forgeops.dev"],
        audience="forgeops-ai",
    )
    mock_jwks_client = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_key
    mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key
    verifier._jwks_clients["https://auth.forgeops.dev"] = mock_jwks_client
    verifier._jwks_cache_times["https://auth.forgeops.dev"] = time.time()
    return verifier


@pytest.fixture()
def ai_deps(
    tier_config: TierConfig,
    mock_registry: EndpointRegistry,
    mock_breakers: dict[str, CircuitBreaker],
    mock_model_router: AsyncMock,
    mock_limiter: AsyncMock,
    mock_verifier: OidcTokenVerifier,
) -> AIDeps:
    """Build AIDeps container."""
    return AIDeps(
        tier_config=tier_config,
        registry=mock_registry,
        breakers=mock_breakers,
        model_router=mock_model_router,
        limiter=mock_limiter,
        verifier=mock_verifier,
    )


@pytest.fixture()
def app(ai_deps: AIDeps) -> FastAPI:
    """Build a test FastAPI app with AI routes and error handlers."""
    from src.core.errors import install_problem_handlers

    app = FastAPI()
    install_problem_handlers(app)
    app.state.ai_deps = ai_deps
    app.include_router(router)
    return app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def valid_token(rsa_keypair) -> str:
    """A valid JWT for our test verifier."""
    private_pem, _ = rsa_keypair
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": "https://auth.forgeops.dev",
            "sub": "user-99",
            "aud": "forgeops-ai",
            "exp": now + 3600,
            "iat": now,
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/ai/tiers
# ---------------------------------------------------------------------------


class TestTiersEndpoint:
    """GET /api/v1/ai/tiers returns 6 tiers with endpoint info."""

    def test_returns_six_tiers(self, client: TestClient):
        resp = client.get("/api/v1/ai/tiers")
        assert resp.status_code == 200
        data = resp.json()
        assert "tiers" in data
        assert len(data["tiers"]) == 6

    def test_tier_info_structure(self, client: TestClient):
        resp = client.get("/api/v1/ai/tiers")
        data = resp.json()
        tier = data["tiers"][0]
        assert "name" in tier
        assert "primary_endpoint" in tier
        assert "primary_protocol" in tier
        assert "available" in tier
        assert "breaker_state" in tier

    def test_tier_names_are_correct(self, client: TestClient):
        resp = client.get("/api/v1/ai/tiers")
        data = resp.json()
        tier_names = {t["name"] for t in data["tiers"]}
        expected = {
            "high_coding",
            "high_analysis",
            "medium",
            "medium_value",
            "low_logs",
            "self_hosted",
        }
        assert tier_names == expected

    def test_openai_compatible_endpoints_are_available(self, client: TestClient):
        resp = client.get("/api/v1/ai/tiers")
        data = resp.json()
        # high_coding primary is gpt-5.6-sol (openai_compatible → available)
        high_coding = next(t for t in data["tiers"] if t["name"] == "high_coding")
        assert high_coding["primary_endpoint"] == "gpt-5.6-sol"
        assert high_coding["primary_protocol"] == "openai_compatible"
        assert high_coding["available"] is True
        assert high_coding["breaker_state"] == "closed"

    def test_native_protocol_endpoints_not_available(self, client: TestClient):
        resp = client.get("/api/v1/ai/tiers")
        data = resp.json()
        # high_analysis primary is claude-fable-5 (anthropic_native → not available)
        high_analysis = next(t for t in data["tiers"] if t["name"] == "high_analysis")
        assert high_analysis["primary_endpoint"] == "claude-fable-5"
        assert high_analysis["primary_protocol"] == "anthropic_native"
        assert high_analysis["available"] is False

    def test_tiers_does_not_require_auth(self, client: TestClient):
        """Tiers listing is public — no auth required."""
        resp = client.get("/api/v1/ai/tiers")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/ai/complete — auth required
# ---------------------------------------------------------------------------


class TestCompleteAuth:
    """POST /api/v1/ai/complete requires auth (401 without token)."""

    def test_no_auth_returns_401(self, client: TestClient):
        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Hello"},
        )
        assert resp.status_code == 401

    def test_empty_auth_returns_401(self, client: TestClient):
        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Hello"},
            headers={"authorization": ""},
        )
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self, client: TestClient):
        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Hello"},
            headers={"authorization": "Bearer bad.token.here!!!"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/ai/complete — valid flow
# ---------------------------------------------------------------------------


class TestCompleteValidFlow:
    """POST /api/v1/ai/complete with valid flow returns routing result."""

    def test_valid_request_returns_200(self, client: TestClient, valid_token: str):
        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Write me a function"},
            headers={"authorization": f"Bearer {valid_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "ok"
        assert data["content"] == "Hello, I can help with that!"
        assert data["endpoint_id"] == "gpt-5.6-sol"
        assert data["served_from"] == "endpoint"
        assert data["degraded"] is False

    def test_unknown_tier_returns_422(self, client: TestClient, valid_token: str):
        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "nonexistent_tier", "prompt": "Hello"},
            headers={"authorization": f"Bearer {valid_token}"},
        )
        assert resp.status_code == 422

    def test_empty_prompt_returns_422(self, client: TestClient, valid_token: str):
        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": ""},
            headers={"authorization": f"Bearer {valid_token}"},
        )
        assert resp.status_code == 422

    def test_router_called_with_correct_tier(self, client: TestClient, valid_token: str, mock_model_router: AsyncMock):
        client.post(
            "/api/v1/ai/complete",
            json={"tier": "medium", "prompt": "Explain this code"},
            headers={"authorization": f"Bearer {valid_token}"},
        )
        mock_model_router.complete.assert_called_once()
        call_kwargs = mock_model_router.complete.call_args.kwargs
        assert call_kwargs["tier"] == ModelTier.MEDIUM


# ---------------------------------------------------------------------------
# Tests: Rate limiter deny → 429
# ---------------------------------------------------------------------------


class TestRateLimiterDeny:
    """Rate limiter deny returns 429 with Retry-After."""

    def test_rate_limited_returns_429(self, client: TestClient, valid_token: str, mock_limiter: AsyncMock):
        mock_limiter.check.return_value = RateLimitDecision(allowed=False, remaining=0, retry_after_seconds=5.0)

        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Hello"},
            headers={"authorization": f"Bearer {valid_token}"},
        )
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert resp.headers["Retry-After"] == "5"

    def test_rate_limited_body_structure(self, client: TestClient, valid_token: str, mock_limiter: AsyncMock):
        mock_limiter.check.return_value = RateLimitDecision(allowed=False, remaining=0, retry_after_seconds=10.0)

        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Hello"},
            headers={"authorization": f"Bearer {valid_token}"},
        )
        data = resp.json()
        assert data["status"] == 429
        assert "rate" in data["title"].lower() or "rate" in data.get("detail", "").lower()


# ---------------------------------------------------------------------------
# Tests: Rate limiter unavailable → 503
# ---------------------------------------------------------------------------


class TestRateLimiterUnavailable:
    """Rate limiter unavailable returns 503 (fail-closed)."""

    def test_redis_failure_returns_503(self, client: TestClient, valid_token: str, mock_limiter: AsyncMock):
        mock_limiter.check.side_effect = RateLimitServiceError("Redis connection refused")

        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Hello"},
            headers={"authorization": f"Bearer {valid_token}"},
        )
        assert resp.status_code == 503

    def test_redis_failure_body_structure(self, client: TestClient, valid_token: str, mock_limiter: AsyncMock):
        mock_limiter.check.side_effect = RateLimitServiceError("Connection timed out")

        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Hello"},
            headers={"authorization": f"Bearer {valid_token}"},
        )
        data = resp.json()
        assert data["status"] == 503


# ---------------------------------------------------------------------------
# Tests: Security/admission order
# ---------------------------------------------------------------------------


class TestAdmissionOrder:
    """Verify the fixed security/admission order: OIDC → sub → limiter → router."""

    def test_auth_checked_before_limiter(self, client: TestClient, mock_limiter: AsyncMock):
        """Without auth, limiter should never be called."""
        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Hello"},
        )
        assert resp.status_code == 401
        # Limiter was NOT consulted
        mock_limiter.check.assert_not_called()

    def test_limiter_checked_before_router(
        self, client: TestClient, valid_token: str, mock_limiter: AsyncMock, mock_model_router: AsyncMock
    ):
        """When limiter denies, router is never called."""
        mock_limiter.check.return_value = RateLimitDecision(allowed=False, remaining=0, retry_after_seconds=2.0)

        resp = client.post(
            "/api/v1/ai/complete",
            json={"tier": "high_coding", "prompt": "Hello"},
            headers={"authorization": f"Bearer {valid_token}"},
        )
        assert resp.status_code == 429
        # Router was NOT invoked
        mock_model_router.complete.assert_not_called()
