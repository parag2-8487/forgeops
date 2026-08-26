# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for Wave 16: gateway tools/call, endpoints, breaker, cache, keys, rate limiter."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from src.core.errors import ProblemException

from tests.synthetic_secrets import openai_style_key

# ===========================================================================
# Task 12.8: tools/call gateway path tests
# ===========================================================================


class TestHandleToolsCall:
    """Tests for McpGateway.handle_tools_call."""

    def _make_gateway(
        self,
        *,
        verifier_claims=None,
        route_server_name="terraform",
        route_server_url="http://localhost:9001",
        route_server_capabilities=None,
        policy_allow=True,
        upstream_result=None,
    ):
        """Build an McpGateway with mocked dependencies."""
        from src.mcp.auth import Claims
        from src.mcp.cache import TtlToolCache
        from src.mcp.gateway import McpGateway
        from src.mcp.policy import OpaGatewayPolicy
        from src.mcp.registry import ServerDescriptor
        from src.mcp.upstream import McpUpstream

        # Verifier
        verifier = AsyncMock()
        if verifier_claims is None:
            verifier_claims = Claims(
                sub="user1",
                iss="https://auth.example.com",
                aud="forgeops-gateway",
                exp=9999999999,
                iat=1000000000,
                raw={},
            )
        verifier.verify = AsyncMock(return_value=verifier_claims)

        # Router
        from src.mcp.routing import Route

        capabilities = (
            route_server_capabilities if route_server_capabilities is not None else ["tools/list", "tools/call"]
        )
        server = ServerDescriptor(
            name=route_server_name,
            url=route_server_url,
            description="Test server",
            capabilities=capabilities,
        )
        route = Route(server=server, method="tools/call", kind="tools_call")
        router = MagicMock()
        router.route = MagicMock(return_value=route)

        # Policy. `spec=` is load-bearing: it makes the double reject a call that
        # the real OpaGatewayPolicy would reject. Behaviour is configured on the
        # spec'd child mock, never by reassigning the attribute — reassigning
        # discards signature enforcement, which is how a broken gateway once
        # passed CI.
        policy = create_autospec(OpaGatewayPolicy, spec_set=True, instance=True)
        if policy_allow:
            policy.authorise_call.return_value = None
        else:
            policy.authorise_call.side_effect = ProblemException(
                status=403,
                type_suffix="mcp-call-denied",
                title="Tool call denied",
                detail="Policy denied.",
            )

        # Cache: consulted by local metadata resolution, never for dispatch.
        cache = create_autospec(TtlToolCache, spec_set=True, instance=True)
        cache.get.return_value = None

        # Upstream
        upstream = create_autospec(McpUpstream, spec_set=True, instance=True)
        upstream.call_tool.return_value = upstream_result or {"content": [{"type": "text", "text": "ok"}]}

        # Registry (not directly used but required)
        registry = MagicMock()

        gateway = McpGateway(
            registry=registry,
            verifier=verifier,
            router=router,
            policy=policy,
            cache=cache,
            upstream=upstream,
            agent_blast_radius="read_only",
        )
        return gateway, verifier, router, policy, upstream

    def _make_body(self, tool_name: str = "plan_apply", arguments: dict | None = None) -> bytes:
        """Build a JSON-RPC tools/call body."""
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {
                    "name": tool_name,
                    "arguments": arguments or {},
                },
            }
        ).encode()

    async def test_allow_path_invokes_upstream(self):
        """Happy path: auth → route → parse → metadata → OPA allow → upstream."""
        gw, verifier, router, policy, upstream = self._make_gateway()
        body = self._make_body("plan_apply", {"target": "prod"})

        result = await gw.handle_tools_call(
            authorization="Bearer token123",
            headers={"Mcp-Method": "tools/call", "Mcp-Name": "terraform"},
            body=body,
        )

        verifier.verify.assert_called_once_with("Bearer token123")
        router.route.assert_called_once()
        policy.authorise_call.assert_called_once()
        upstream.call_tool.assert_called_once()
        assert result == {"content": [{"type": "text", "text": "ok"}]}

    async def test_deny_path_raises_403(self):
        """OPA deny → 403 before upstream is reached."""
        gw, verifier, router, policy, upstream = self._make_gateway(policy_allow=False)
        body = self._make_body("plan_apply")

        with pytest.raises(ProblemException) as exc_info:
            await gw.handle_tools_call(
                authorization="Bearer token123",
                headers={"Mcp-Method": "tools/call", "Mcp-Name": "terraform"},
                body=body,
            )

        assert exc_info.value.problem.status == 403
        upstream.call_tool.assert_not_called()

    async def test_missing_tool_name_raises_400(self):
        """Body without params.name → 400."""
        gw, _, _, _, _ = self._make_gateway()
        body = json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {}}).encode()

        with pytest.raises(ProblemException) as exc_info:
            await gw.handle_tools_call(
                authorization="Bearer token123",
                headers={"Mcp-Method": "tools/call", "Mcp-Name": "terraform"},
                body=body,
            )

        assert exc_info.value.problem.status == 400
        assert "mcp-missing-tool-name" in exc_info.value.problem.type

    async def test_invalid_json_body_raises_400(self):
        """Non-JSON body → 400."""
        gw, _, _, _, _ = self._make_gateway()

        with pytest.raises(ProblemException) as exc_info:
            await gw.handle_tools_call(
                authorization="Bearer token123",
                headers={"Mcp-Method": "tools/call", "Mcp-Name": "terraform"},
                body=b"not json!",
            )

        assert exc_info.value.problem.status == 400
        assert "mcp-invalid-body" in exc_info.value.problem.type

    async def test_missing_metadata_raises_404(self):
        """Server that doesn't support tools/call → 404."""
        gw, _, _, _, _ = self._make_gateway(route_server_capabilities=["tools/list"])
        body = self._make_body("some_tool")

        with pytest.raises(ProblemException) as exc_info:
            await gw.handle_tools_call(
                authorization="Bearer token123",
                headers={"Mcp-Method": "tools/call", "Mcp-Name": "terraform"},
                body=body,
            )

        assert exc_info.value.problem.status == 404
        assert "mcp-tool-not-found" in exc_info.value.problem.type


# ===========================================================================
# Task 13.2: EndpointRegistry, OpenAICompatibleEndpoint tests
# ===========================================================================


class TestEndpointRegistry:
    """Tests for EndpointRegistry and OpenAICompatibleEndpoint."""

    def _make_config(self):
        """Build a minimal TierConfig for testing."""
        from src.ai.routing.tiers import (
            EndpointDescriptor,
            EndpointProtocol,
            ModelTier,
            TierChain,
            TierConfig,
        )

        endpoints = {
            "gpt-5.6-sol": EndpointDescriptor(
                id="gpt-5.6-sol",
                provider="openai",
                model="gpt-5.6-sol",
                protocol=EndpointProtocol.OPENAI_COMPATIBLE,
                base_url="https://api.openai.com/v1",
                key_ref="openai",
                timeout_seconds=60.0,
            ),
            "claude-fable-5": EndpointDescriptor(
                id="claude-fable-5",
                provider="anthropic",
                model="claude-fable-5",
                protocol=EndpointProtocol.ANTHROPIC_NATIVE,
                base_url="https://api.anthropic.com/v1",
                key_ref="anthropic",
                timeout_seconds=60.0,
            ),
            "gemini-3-flash": EndpointDescriptor(
                id="gemini-3-flash",
                provider="google",
                model="gemini-3-flash",
                protocol=EndpointProtocol.GOOGLE_NATIVE,
                base_url="https://generativelanguage.googleapis.com",
                key_ref="google",
                timeout_seconds=60.0,
            ),
        }
        tiers = {
            ModelTier.HIGH_CODING: TierChain(primary="gpt-5.6-sol", secondary="claude-fable-5"),
        }
        return TierConfig(tiers=tiers, endpoints=endpoints)

    def test_from_config_creates_openai_compatible(self):
        """OpenAI-compatible endpoints are instantiated."""
        from src.ai.routing.endpoints import EndpointRegistry, OpenAICompatibleEndpoint

        config = self._make_config()
        registry = EndpointRegistry.from_config(config)

        ep = registry.endpoint("gpt-5.6-sol")
        assert ep is not None
        assert isinstance(ep, OpenAICompatibleEndpoint)
        assert ep.endpoint_id == "gpt-5.6-sol"
        assert ep.provider_kind == "openai"

    def test_native_protocols_marked_unavailable(self):
        """Anthropic and Google native protocols are marked unavailable."""
        from src.ai.routing.endpoints import EndpointRegistry

        config = self._make_config()
        registry = EndpointRegistry.from_config(config)

        # Native endpoints are not instantiated
        assert registry.endpoint("claude-fable-5") is None
        assert registry.endpoint("gemini-3-flash") is None

        # But availability info exists with reason
        avail_claude = registry.get_availability("claude-fable-5")
        assert avail_claude is not None
        assert avail_claude.available is False
        assert avail_claude.reason == "unsupported_protocol_phase_0"

        avail_gemini = registry.get_availability("gemini-3-flash")
        assert avail_gemini is not None
        assert avail_gemini.available is False
        assert avail_gemini.reason == "unsupported_protocol_phase_0"

    def test_openai_compatible_marked_available(self):
        """OpenAI-compatible endpoints are marked available."""
        from src.ai.routing.endpoints import EndpointRegistry

        config = self._make_config()
        registry = EndpointRegistry.from_config(config)

        avail = registry.get_availability("gpt-5.6-sol")
        assert avail is not None
        assert avail.available is True
        assert avail.reason is None

    async def test_openai_compatible_complete(self):
        """OpenAICompatibleEndpoint sends to /chat/completions."""
        from src.ai.routing.endpoints import (
            CompletionRequest,
            CompletionResponse,
            OpenAICompatibleEndpoint,
        )
        from src.ai.routing.tiers import EndpointDescriptor, EndpointProtocol

        descriptor = EndpointDescriptor(
            id="test-ep",
            provider="openai",
            model="gpt-test",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="https://api.openai.com/v1",
            key_ref="openai",
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(
            return_value={
                "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
                "model": "gpt-test",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

        http = AsyncMock()
        http.post = AsyncMock(return_value=mock_response)

        ep = OpenAICompatibleEndpoint(descriptor=descriptor, http=http)
        request = CompletionRequest(
            model="gpt-test",
            messages=[{"role": "user", "content": "Hi"}],
        )

        result = await ep.complete(request, credential=openai_style_key())
        assert isinstance(result, CompletionResponse)
        assert result.content == "Hello!"
        assert result.model == "gpt-test"
        assert result.usage["total_tokens"] == 15

        # Verify the request was sent correctly
        http.post.assert_called_once()
        call_args = http.post.call_args
        assert "/chat/completions" in call_args[0][0]

    def test_endpoint_not_found_returns_none(self):
        """Registry returns None for unknown endpoint IDs."""
        from src.ai.routing.endpoints import EndpointRegistry

        config = self._make_config()
        registry = EndpointRegistry.from_config(config)
        assert registry.endpoint("nonexistent") is None


# ===========================================================================
# Task 13.3: Circuit breaker tests
# ===========================================================================


class TestCircuitBreaker:
    """Tests for the per-endpoint circuit breaker."""

    def test_initial_state_is_closed(self):
        from src.ai.routing.breaker import BreakerState, CircuitBreaker

        breaker = CircuitBreaker(clock=lambda: 0.0)
        assert breaker.state() == BreakerState.CLOSED
        assert breaker.allows() is True

    def test_threshold_trip(self):
        """5 failures within 30s window trips the breaker to OPEN."""
        from src.ai.routing.breaker import BreakerState, CircuitBreaker

        now = 100.0
        breaker = CircuitBreaker(clock=lambda: now, failure_threshold=5, failure_window_seconds=30.0)

        for _ in range(4):
            breaker.record_failure()
            assert breaker.state() == BreakerState.CLOSED
            assert breaker.allows() is True

        # 5th failure trips it
        breaker.record_failure()
        assert breaker.state() == BreakerState.OPEN
        assert breaker.allows() is False

    def test_cooldown_transitions_to_half_open(self):
        """After 60s cooldown, breaker transitions from OPEN to HALF_OPEN."""
        from src.ai.routing.breaker import BreakerState, CircuitBreaker

        current_time = [100.0]
        breaker = CircuitBreaker(
            clock=lambda: current_time[0],
            failure_threshold=5,
            cooldown_seconds=60.0,
        )

        # Trip the breaker
        for _ in range(5):
            breaker.record_failure()
        assert breaker.state() == BreakerState.OPEN

        # Advance past cooldown
        current_time[0] = 161.0  # 61s later
        assert breaker.state() == BreakerState.HALF_OPEN

    def test_half_open_probe_allowed(self):
        """In HALF_OPEN, exactly one probe request is allowed."""
        from src.ai.routing.breaker import BreakerState, CircuitBreaker

        current_time = [100.0]
        breaker = CircuitBreaker(
            clock=lambda: current_time[0],
            failure_threshold=5,
            cooldown_seconds=60.0,
        )

        # Trip and advance to half_open
        for _ in range(5):
            breaker.record_failure()
        current_time[0] = 161.0
        assert breaker.state() == BreakerState.HALF_OPEN

        # First allows() returns True (the probe)
        assert breaker.allows() is True
        # Second allows() returns False (only one probe)
        assert breaker.allows() is False

    def test_probe_success_resets_to_closed(self):
        """Successful probe in HALF_OPEN → CLOSED."""
        from src.ai.routing.breaker import BreakerState, CircuitBreaker

        current_time = [100.0]
        breaker = CircuitBreaker(
            clock=lambda: current_time[0],
            failure_threshold=5,
            cooldown_seconds=60.0,
        )

        for _ in range(5):
            breaker.record_failure()
        current_time[0] = 161.0
        assert breaker.state() == BreakerState.HALF_OPEN

        breaker.allows()  # take the probe slot
        breaker.record_success()

        assert breaker.state() == BreakerState.CLOSED
        assert breaker.allows() is True

    def test_probe_failure_returns_to_open(self):
        """Failed probe in HALF_OPEN → back to OPEN."""
        from src.ai.routing.breaker import BreakerState, CircuitBreaker

        current_time = [100.0]
        breaker = CircuitBreaker(
            clock=lambda: current_time[0],
            failure_threshold=5,
            cooldown_seconds=60.0,
        )

        for _ in range(5):
            breaker.record_failure()
        current_time[0] = 161.0
        assert breaker.state() == BreakerState.HALF_OPEN

        breaker.allows()  # take the probe slot
        breaker.record_failure()

        assert breaker.state() == BreakerState.OPEN
        assert breaker.allows() is False

    def test_failures_outside_window_are_evicted(self):
        """Failures older than the window are not counted."""
        from src.ai.routing.breaker import BreakerState, CircuitBreaker

        current_time = [100.0]
        breaker = CircuitBreaker(
            clock=lambda: current_time[0],
            failure_threshold=5,
            failure_window_seconds=30.0,
        )

        # 4 failures at t=100
        for _ in range(4):
            breaker.record_failure()
        assert breaker.state() == BreakerState.CLOSED

        # Advance past window for those failures
        current_time[0] = 131.0  # 31s later
        # 1 more failure — old ones evicted, total in window = 1
        breaker.record_failure()
        assert breaker.state() == BreakerState.CLOSED


# ===========================================================================
# Task 13.4: Tiered semantic cache tests
# ===========================================================================


class TestTieredSemanticCache:
    """Tests for L1 exact-match cache."""

    def _make_fake_redis(self):
        """Build an in-memory fake Redis supporting get/set."""

        class FakeRedis:
            def __init__(self):
                self._store: dict[str, tuple[str | bytes, int | None]] = {}

            async def get(self, key: str) -> bytes | None:
                if key not in self._store:
                    return None
                val, _ = self._store[key]
                return val.encode() if isinstance(val, str) else val

            async def set(self, key: str, value: str | bytes, *, ex: int | None = None) -> None:
                self._store[key] = (value, ex)

        return FakeRedis()

    async def test_cache_miss(self):
        """Lookup returns None on miss."""
        from src.ai.routing.cache import TieredSemanticCache
        from src.secrets.redaction import create_redacted_chunk

        redis = self._make_fake_redis()
        cache = TieredSemanticCache(redis=redis)

        result = await cache.lookup(
            model="gpt-4",
            prompt=create_redacted_chunk("hello"),
        )
        assert result is None

    async def test_cache_hit(self):
        """Store then lookup returns CacheHit."""
        from src.ai.routing.cache import CacheHit, TieredSemanticCache
        from src.secrets.redaction import create_redacted_chunk

        redis = self._make_fake_redis()
        cache = TieredSemanticCache(redis=redis)

        prompt = create_redacted_chunk("hello")
        await cache.store(model="gpt-4", prompt=prompt, content="Hi there!")

        hit = await cache.lookup(model="gpt-4", prompt=prompt)
        assert hit is not None
        assert isinstance(hit, CacheHit)
        assert hit.served_from == "L1_exact"
        assert hit.content == "Hi there!"
        assert hit.degraded is False

    async def test_different_params_different_key(self):
        """Different params produce different cache keys."""
        from src.ai.routing.cache import TieredSemanticCache
        from src.secrets.redaction import create_redacted_chunk

        redis = self._make_fake_redis()
        cache = TieredSemanticCache(redis=redis)

        prompt = create_redacted_chunk("hello")
        await cache.store(model="gpt-4", prompt=prompt, params={"temperature": 0.5}, content="A")

        # Different params → miss
        result = await cache.lookup(model="gpt-4", prompt=prompt, params={"temperature": 0.9})
        assert result is None

        # Same params → hit
        result = await cache.lookup(model="gpt-4", prompt=prompt, params={"temperature": 0.5})
        assert result is not None
        assert result.content == "A"

    async def test_store_with_custom_ttl(self):
        """TTL is passed to Redis set."""
        redis = self._make_fake_redis()
        from src.ai.routing.cache import TieredSemanticCache
        from src.secrets.redaction import create_redacted_chunk

        cache = TieredSemanticCache(redis=redis, default_ttl_seconds=3600)
        prompt = create_redacted_chunk("test")
        await cache.store(model="gpt-4", prompt=prompt, content="resp", ttl_seconds=120)

        # Check the stored TTL
        for _key, (_val, ttl) in redis._store.items():
            assert ttl == 120


# ===========================================================================
# Task 13.5: BYO-key resolver tests
# ===========================================================================


class TestKeyResolver:
    """Tests for EnvKeyResolver."""

    def test_resolve_from_env(self):
        """EnvKeyResolver reads LLM_KEY_OPENAI from env."""
        from src.ai.routing.keys import EnvKeyResolver, SecretValue

        with patch.dict(os.environ, {"LLM_KEY_OPENAI": "sk-test-key-123"}):
            resolver = EnvKeyResolver()
            result = resolver.resolve("openai")

        assert result is not None
        assert isinstance(result, SecretValue)
        assert result.get_secret_value() == "sk-test-key-123"

    def test_resolve_missing_key_returns_none(self):
        """Missing env var returns None."""
        from src.ai.routing.keys import EnvKeyResolver

        with patch.dict(os.environ, {}, clear=True):
            resolver = EnvKeyResolver()
            # Ensure the key doesn't exist
            os.environ.pop("LLM_KEY_NONEXISTENT", None)
            result = resolver.resolve("nonexistent")

        assert result is None

    def test_resolve_empty_key_returns_none(self):
        """Empty env var returns None."""
        from src.ai.routing.keys import EnvKeyResolver

        with patch.dict(os.environ, {"LLM_KEY_EMPTY": "  "}):
            resolver = EnvKeyResolver()
            result = resolver.resolve("empty")

        assert result is None

    def test_secret_value_never_leaks_in_repr(self):
        """SecretValue repr/str never exposes the actual value."""
        from src.ai.routing.keys import SecretValue

        secret = SecretValue("super-secret-key")
        assert "super-secret-key" not in repr(secret)
        assert "super-secret-key" not in str(secret)
        assert "***" in repr(secret)
        assert "***" in str(secret)

    def test_secret_value_get_secret_value(self):
        """get_secret_value() returns the actual secret."""
        from src.ai.routing.keys import SecretValue

        secret = SecretValue("my-key")
        assert secret.get_secret_value() == "my-key"

    def test_custom_prefix(self):
        """EnvKeyResolver supports custom prefix."""
        from src.ai.routing.keys import EnvKeyResolver

        with patch.dict(os.environ, {"CUSTOM_OPENAI": "custom-key"}):
            resolver = EnvKeyResolver(prefix="CUSTOM_")
            result = resolver.resolve("openai")

        assert result is not None
        assert result.get_secret_value() == "custom-key"


# ===========================================================================
# Task 13.6: Redis/Lua token bucket rate limiter tests
# ===========================================================================


class TestRedisTokenBucketLimiter:
    """Tests for RedisTokenBucketLimiter."""

    def _make_fake_redis(self, *, lua_result=None, raise_exc=None):
        """Fake Redis that returns a predetermined Lua result."""

        class FakeLuaRedis:
            def __init__(self):
                self.calls: list[tuple] = []

            async def eval(self, script: str, numkeys: int, *keys_and_args):
                self.calls.append((script, numkeys, keys_and_args))
                if raise_exc:
                    raise raise_exc
                return lua_result

        return FakeLuaRedis()

    async def test_allow_decision(self):
        """Lua returns [1, remaining, 0] → allowed=True."""
        from src.ai.rate_limit import RateLimitDecision, RedisTokenBucketLimiter

        redis = self._make_fake_redis(lua_result=[1, 99, 0])
        limiter = RedisTokenBucketLimiter(redis=redis, capacity=100, refill_rate=10.0)

        decision = await limiter.check("user:123")
        assert isinstance(decision, RateLimitDecision)
        assert decision.allowed is True
        assert decision.remaining == 99
        assert decision.retry_after_seconds is None

    async def test_deny_decision_with_retry_after(self):
        """Lua returns [0, 0, 5000] → allowed=False, retry_after=5.0s."""
        from src.ai.rate_limit import RedisTokenBucketLimiter

        redis = self._make_fake_redis(lua_result=[0, 0, 5000])
        limiter = RedisTokenBucketLimiter(redis=redis, capacity=100, refill_rate=10.0)

        decision = await limiter.check("user:123")
        assert decision.allowed is False
        assert decision.remaining == 0
        assert decision.retry_after_seconds == 5.0

    async def test_fail_closed_on_redis_error(self):
        """Redis failure → RateLimitServiceError (503 semantics)."""
        from src.ai.rate_limit import RedisTokenBucketLimiter
        from src.ai.rate_limit.redis_bucket import RateLimitServiceError

        redis = self._make_fake_redis(raise_exc=ConnectionError("Redis down"))
        limiter = RedisTokenBucketLimiter(redis=redis, capacity=100, refill_rate=10.0)

        with pytest.raises(RateLimitServiceError):
            await limiter.check("user:123")

    async def test_custom_bucket_key_prefix(self):
        """Custom key prefix is used in Redis eval."""
        from src.ai.rate_limit import RedisTokenBucketLimiter

        redis = self._make_fake_redis(lua_result=[1, 50, 0])
        limiter = RedisTokenBucketLimiter(redis=redis, capacity=100, refill_rate=10.0, key_prefix="custom:rl:")

        await limiter.check("tenant:abc")
        # Check the key passed to eval
        _, _, keys_and_args = redis.calls[0]
        assert keys_and_args[0] == "custom:rl:tenant:abc"

    async def test_multiple_tokens_consumed(self):
        """Can consume multiple tokens at once."""
        from src.ai.rate_limit import RedisTokenBucketLimiter

        redis = self._make_fake_redis(lua_result=[1, 90, 0])
        limiter = RedisTokenBucketLimiter(redis=redis, capacity=100, refill_rate=10.0)

        decision = await limiter.check("user:123", tokens=10)
        assert decision.allowed is True
        assert decision.remaining == 90
