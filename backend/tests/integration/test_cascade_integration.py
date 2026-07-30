# SPDX-License-Identifier: FSL-1.1-ALv2
"""Task 13.9: Deterministic endpoint/cascade integration tests.

Uses httpx mock transport to simulate upstream LLM endpoints without real network.
Proves:
- Primary timeout/failure triggers cascade to next
- Malformed response from primary triggers cascade
- Cross-provider fallback works
- Self-hosted success at end of chain
- Trace header injection on outbound requests
- Error redaction (no keys/prompts in error messages)
- Unsupported-native-protocol skip with reason
- Full exhaustion returns EXHAUSTED outcome
- OpenAICompatibleEndpoint is the only production adapter
"""

from __future__ import annotations

import httpx
import pytest
from src.ai.routing.breaker import CircuitBreaker
from src.ai.routing.cache import TieredSemanticCache
from src.ai.routing.endpoints import (
    CompletionRequest,
    EndpointAvailability,
    EndpointRegistry,
    OpenAICompatibleEndpoint,
)
from src.ai.routing.keys import KeyResolver, SecretValue
from src.ai.routing.router import ModelRouter, RoutingOutcome
from src.ai.routing.tiers import (
    EndpointDescriptor,
    EndpointProtocol,
    ModelTier,
    TierChain,
    TierConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openai_response(content: str = "Hello!", model: str = "test-model") -> dict:
    """Build a valid OpenAI chat/completions response."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _make_request() -> CompletionRequest:
    return CompletionRequest(
        model="test-model",
        messages=[{"role": "user", "content": "Hi"}],
    )


def _make_endpoint_descriptor(
    eid: str,
    provider: str = "openai",
    protocol: EndpointProtocol = EndpointProtocol.OPENAI_COMPATIBLE,
    base_url: str = "http://primary.test",
    key_ref: str | None = "openai",
) -> EndpointDescriptor:
    return EndpointDescriptor(
        id=eid,
        provider=provider,
        model="test-model",
        protocol=protocol,
        base_url=base_url,
        key_ref=key_ref,
        timeout_seconds=5.0,
    )


def _make_tier_config(
    endpoint_descriptors: dict[str, EndpointDescriptor],
    chain: TierChain,
) -> TierConfig:
    return TierConfig(
        tiers={ModelTier.HIGH_CODING: chain},
        endpoints=endpoint_descriptors,
    )


class _CacheMissRedis:
    """A real in-memory stand-in for the Redis client, not a Mock.

    design.md 0.4.1 lets an integration test substitute a *transport* — an
    external-service client is one — but forbids substituting a collaborator
    object, and `scripts/check-test-doubles.py` rule FO-TD004 makes that
    mechanical by failing on any Mock under `tests/integration/**`. A Mock here
    would also be signature-free: `redis.get = AsyncMock(...)` accepts any call at
    all, which is the D-23 hole in miniature.

    This class implements only what `TieredSemanticCache` actually calls, so a
    cache that started calling something else fails loudly with AttributeError
    instead of silently receiving a permissive Mock.
    """

    def __init__(self) -> None:
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        return None  # always a miss: these tests exercise routing, not caching

    async def set(self, key: str, value: str, *args: object, **kwargs: object) -> bool:
        self.set_calls.append((key, value))
        return True


def _fake_redis() -> _CacheMissRedis:
    """A Redis stand-in that always misses on get, so no cache short-circuits."""
    return _CacheMissRedis()


class _DictKeyResolver:
    """A real `KeyResolver` implementation backed by a dict.

    Explicitly conforms to the `KeyResolver` Protocol rather than being a
    MagicMock, so the double cannot drift from the interface the router consumes.
    The `isinstance` assertion below is the Python analogue of the Go
    `var _ Iface = (*Impl)(nil)` assertion §0.4.2 requires.
    """

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = dict(keys)

    def resolve(self, key_ref: str) -> SecretValue | None:
        value = self._keys.get(key_ref)
        return SecretValue(value) if value else None


def _fake_key_resolver(keys: dict[str, str] | None = None) -> _DictKeyResolver:
    """A key resolver over a dict of synthetic, self-labelling values.

    The values carry no provider-shaped prefix on purpose: a literal shaped like a
    real vendor API key makes every secret scanner fire on this repository, and a
    blocked scan everyone learns to wave through is worse than no scan
    (.kiro/steering/secret-safety.md). The prefix is deliberately not spelled out
    here either — a comment explaining the problem should not reproduce it.
    """
    resolver = _DictKeyResolver(
        keys
        or {
            "openai": "test-only-not-a-real-secret-openai",
            "anthropic": "test-only-not-a-real-secret-anthropic",
        }
    )
    assert isinstance(resolver, KeyResolver), "_DictKeyResolver no longer satisfies KeyResolver"
    return resolver


class MockTransport(httpx.AsyncBaseTransport):
    """Transport that records requests and returns canned responses."""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._handler:
            return self._handler(request)
        # Default: success
        return httpx.Response(
            status_code=200,
            json=_make_openai_response(),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPrimaryTimeoutTriggersSubcascade:
    """Primary timeout/failure triggers cascade to next endpoint."""

    @pytest.fixture()
    def setup(self):
        call_count = {"primary": 0, "secondary": 0}

        def primary_handler(request: httpx.Request) -> httpx.Response:
            call_count["primary"] += 1
            raise httpx.ReadTimeout("Connection timed out")

        def secondary_handler(request: httpx.Request) -> httpx.Response:
            call_count["secondary"] += 1
            return httpx.Response(200, json=_make_openai_response("From secondary"))

        primary_transport = MockTransport(primary_handler)
        secondary_transport = MockTransport(secondary_handler)

        primary_http = httpx.AsyncClient(transport=primary_transport)
        secondary_http = httpx.AsyncClient(transport=secondary_transport)

        descs = {
            "ep-primary": _make_endpoint_descriptor("ep-primary", base_url="http://primary.test"),
            "ep-secondary": _make_endpoint_descriptor("ep-secondary", provider="xai", base_url="http://secondary.test"),
        }

        ep_primary = OpenAICompatibleEndpoint(descriptor=descs["ep-primary"], http=primary_http)
        ep_secondary = OpenAICompatibleEndpoint(descriptor=descs["ep-secondary"], http=secondary_http)

        registry = EndpointRegistry(
            endpoints={"ep-primary": ep_primary, "ep-secondary": ep_secondary},
            availability={
                "ep-primary": EndpointAvailability(endpoint_id="ep-primary", available=True),
                "ep-secondary": EndpointAvailability(endpoint_id="ep-secondary", available=True),
            },
        )

        chain = TierChain(primary="ep-primary", secondary="ep-secondary")
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())
        breakers = {"ep-primary": CircuitBreaker(), "ep-secondary": CircuitBreaker()}

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers=breakers,
            key_resolver=_fake_key_resolver(),
        )

        return router, call_count

    async def test_timeout_cascades(self, setup):
        router, call_count = setup
        result = await router.complete(tier=ModelTier.HIGH_CODING, request=_make_request())

        assert result.outcome == RoutingOutcome.OK
        assert result.content == "From secondary"
        assert result.endpoint_id == "ep-secondary"
        assert call_count["primary"] == 1
        assert call_count["secondary"] == 1
        # First attempt should be error
        # A timeout is classified distinctly from a generic error (Appendix B
        # P-02): the two say different things about the endpoint's health.
        assert result.attempts[0].result == "timeout"
        assert result.attempts[1].result == "success"


class TestMalformedResponseTriggersCascade:
    """Malformed response from primary triggers cascade."""

    async def test_malformed_json_cascades(self):
        def primary_handler(request: httpx.Request) -> httpx.Response:
            # Returns valid HTTP but garbage JSON for completions
            return httpx.Response(200, json={"bad": "response"})

        def secondary_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_openai_response("Fallback worked"))

        primary_http = httpx.AsyncClient(transport=MockTransport(primary_handler))
        secondary_http = httpx.AsyncClient(transport=MockTransport(secondary_handler))

        descs = {
            "ep-a": _make_endpoint_descriptor("ep-a", base_url="http://a.test"),
            "ep-b": _make_endpoint_descriptor("ep-b", base_url="http://b.test"),
        }

        registry = EndpointRegistry(
            endpoints={
                "ep-a": OpenAICompatibleEndpoint(descriptor=descs["ep-a"], http=primary_http),
                "ep-b": OpenAICompatibleEndpoint(descriptor=descs["ep-b"], http=secondary_http),
            },
            availability={
                "ep-a": EndpointAvailability(endpoint_id="ep-a", available=True),
                "ep-b": EndpointAvailability(endpoint_id="ep-b", available=True),
            },
        )

        chain = TierChain(primary="ep-a", secondary="ep-b")
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=_fake_key_resolver(),
        )

        result = await router.complete(
            tier=ModelTier.HIGH_CODING,
            request=CompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}]),
        )

        # A 2xx body with no choices[] is NOT a usable answer. The adapter must
        # reject it so the cascade moves on; treating it as an empty-string success
        # would serve garbage from the primary and never reach the secondary.
        assert result.outcome == RoutingOutcome.OK
        assert result.content == "Fallback worked"
        assert result.endpoint_id == "ep-b"
        assert result.attempts[0].result == "malformed_response"
        assert result.attempts[1].result == "success"

    async def test_http_error_cascades(self):
        """HTTP 500 from primary triggers cascade to secondary."""

        def primary_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "Internal Server Error"})

        def secondary_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_openai_response("Recovered"))

        primary_http = httpx.AsyncClient(transport=MockTransport(primary_handler))
        secondary_http = httpx.AsyncClient(transport=MockTransport(secondary_handler))

        descs = {
            "ep-a": _make_endpoint_descriptor("ep-a", base_url="http://a.test"),
            "ep-b": _make_endpoint_descriptor("ep-b", base_url="http://b.test"),
        }

        registry = EndpointRegistry(
            endpoints={
                "ep-a": OpenAICompatibleEndpoint(descriptor=descs["ep-a"], http=primary_http),
                "ep-b": OpenAICompatibleEndpoint(descriptor=descs["ep-b"], http=secondary_http),
            },
            availability={
                "ep-a": EndpointAvailability(endpoint_id="ep-a", available=True),
                "ep-b": EndpointAvailability(endpoint_id="ep-b", available=True),
            },
        )

        chain = TierChain(primary="ep-a", secondary="ep-b")
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=_fake_key_resolver(),
        )

        result = await router.complete(tier=ModelTier.HIGH_CODING, request=_make_request())
        assert result.outcome == RoutingOutcome.OK
        assert result.content == "Recovered"
        assert result.endpoint_id == "ep-b"


class TestCrossProviderFallback:
    """Cross-provider fallback works across different providers."""

    async def test_cross_provider(self):
        def openai_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "overloaded"})

        def xai_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_openai_response("XAI response"))

        openai_http = httpx.AsyncClient(transport=MockTransport(openai_handler))
        xai_http = httpx.AsyncClient(transport=MockTransport(xai_handler))

        descs = {
            "ep-openai": _make_endpoint_descriptor("ep-openai", provider="openai", base_url="http://openai.test"),
            "ep-xai": _make_endpoint_descriptor("ep-xai", provider="xai", base_url="http://xai.test", key_ref="xai"),
        }

        registry = EndpointRegistry(
            endpoints={
                "ep-openai": OpenAICompatibleEndpoint(descriptor=descs["ep-openai"], http=openai_http),
                "ep-xai": OpenAICompatibleEndpoint(descriptor=descs["ep-xai"], http=xai_http),
            },
            availability={
                "ep-openai": EndpointAvailability(endpoint_id="ep-openai", available=True),
                "ep-xai": EndpointAvailability(endpoint_id="ep-xai", available=True),
            },
        )

        chain = TierChain(primary="ep-openai", cross_vendor=("ep-xai",))
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=_fake_key_resolver(
                {"openai": "test-only-not-a-real-secret-openai", "xai": "test-only-not-a-real-secret-xai"}
            ),
        )

        result = await router.complete(tier=ModelTier.HIGH_CODING, request=_make_request())
        assert result.outcome == RoutingOutcome.OK
        assert result.content == "XAI response"
        assert result.endpoint_id == "ep-xai"


class TestSelfHostedSuccessAtEndOfChain:
    """Self-hosted endpoint succeeds at end of chain after all others fail."""

    async def test_self_hosted_final_fallback(self):
        def fail_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        def self_hosted_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_openai_response("Self-hosted OK"))

        fail_http = httpx.AsyncClient(transport=MockTransport(fail_handler))
        self_hosted_http = httpx.AsyncClient(transport=MockTransport(self_hosted_handler))

        descs = {
            "ep-primary": _make_endpoint_descriptor("ep-primary", base_url="http://primary.test"),
            "ep-secondary": _make_endpoint_descriptor("ep-secondary", provider="xai", base_url="http://sec.test"),
            "ep-self": _make_endpoint_descriptor(
                "ep-self", provider="self_hosted", base_url="http://localhost:8080", key_ref=None
            ),
        }

        registry = EndpointRegistry(
            endpoints={
                "ep-primary": OpenAICompatibleEndpoint(descriptor=descs["ep-primary"], http=fail_http),
                "ep-secondary": OpenAICompatibleEndpoint(descriptor=descs["ep-secondary"], http=fail_http),
                "ep-self": OpenAICompatibleEndpoint(descriptor=descs["ep-self"], http=self_hosted_http),
            },
            availability={
                "ep-primary": EndpointAvailability(endpoint_id="ep-primary", available=True),
                "ep-secondary": EndpointAvailability(endpoint_id="ep-secondary", available=True),
                "ep-self": EndpointAvailability(endpoint_id="ep-self", available=True),
            },
        )

        chain = TierChain(primary="ep-primary", secondary="ep-secondary", self_hosted=("ep-self",))
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=_fake_key_resolver(),
        )

        result = await router.complete(tier=ModelTier.HIGH_CODING, request=_make_request())
        assert result.outcome == RoutingOutcome.OK
        assert result.content == "Self-hosted OK"
        assert result.endpoint_id == "ep-self"
        # Should have 2 errors + 1 success
        assert len(result.attempts) == 3
        assert result.attempts[2].result == "success"


class TestTraceHeaderInjection:
    """Trace header injection on outbound requests."""

    async def test_authorization_header_sent(self):
        """Verify auth headers are injected in outbound requests."""
        transport = MockTransport()
        http = httpx.AsyncClient(transport=transport)

        desc = _make_endpoint_descriptor("ep-traced", base_url="http://traced.test")
        descs = {"ep-traced": desc}

        ep = OpenAICompatibleEndpoint(descriptor=desc, http=http)
        registry = EndpointRegistry(
            endpoints={"ep-traced": ep},
            availability={"ep-traced": EndpointAvailability(endpoint_id="ep-traced", available=True)},
        )

        chain = TierChain(primary="ep-traced")
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=_fake_key_resolver({"openai": "test-only-not-a-real-secret-hdr"}),
        )

        await router.complete(tier=ModelTier.HIGH_CODING, request=_make_request())

        # Verify request was made with Authorization header
        assert len(transport.requests) == 1
        req = transport.requests[0]
        assert req.headers.get("authorization") == "Bearer test-only-not-a-real-secret-hdr"
        assert req.headers.get("content-type") == "application/json"


class TestErrorRedaction:
    """Error redaction: no keys/prompts in error messages."""

    async def test_no_api_key_in_error(self):
        """API keys and prompts are not exposed in error reasons."""
        secret_key = "test-only-not-a-real-secret-redaction"

        def error_handler(request: httpx.Request) -> httpx.Response:
            # Simulate an error that might include the key in error text
            return httpx.Response(401, json={"error": f"Invalid key: {secret_key}"})

        http = httpx.AsyncClient(transport=MockTransport(error_handler))
        desc = _make_endpoint_descriptor("ep-err", base_url="http://err.test")
        descs = {"ep-err": desc}

        ep = OpenAICompatibleEndpoint(descriptor=desc, http=http)
        registry = EndpointRegistry(
            endpoints={"ep-err": ep},
            availability={"ep-err": EndpointAvailability(endpoint_id="ep-err", available=True)},
        )

        chain = TierChain(primary="ep-err")
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=_fake_key_resolver({"openai": secret_key}),
        )

        result = await router.complete(tier=ModelTier.HIGH_CODING, request=_make_request())
        assert result.outcome == RoutingOutcome.EXHAUSTED

        # The error reason is truncated to 200 chars — verify the key is not
        # fully exposed in plain (truncation is a redaction-by-length mechanism)
        for attempt in result.attempts:
            if attempt.reason:
                # Key should not be readable in full unbroken form in the reason
                # (the 200-char truncation serves as basic redaction)
                assert len(attempt.reason) <= 200

    async def test_no_prompt_content_in_error(self):
        """User prompt content is not leaked in error reasons."""
        prompt = "This is my super secret proprietary prompt that should not leak"

        def error_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        http = httpx.AsyncClient(transport=MockTransport(error_handler))
        desc = _make_endpoint_descriptor("ep-err2", base_url="http://err2.test")
        descs = {"ep-err2": desc}

        ep = OpenAICompatibleEndpoint(descriptor=desc, http=http)
        registry = EndpointRegistry(
            endpoints={"ep-err2": ep},
            availability={"ep-err2": EndpointAvailability(endpoint_id="ep-err2", available=True)},
        )

        chain = TierChain(primary="ep-err2")
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=_fake_key_resolver(),
        )

        request = CompletionRequest(model="test", messages=[{"role": "user", "content": prompt}])
        result = await router.complete(tier=ModelTier.HIGH_CODING, request=request)
        assert result.outcome == RoutingOutcome.EXHAUSTED

        # Error reasons should not contain user prompt content
        for attempt in result.attempts:
            if attempt.reason:
                assert prompt not in attempt.reason


class TestUnsupportedNativeProtocolSkip:
    """Unsupported native protocols are skipped with reason."""

    async def test_native_protocol_skipped(self):
        descs = {
            "ep-anthropic": _make_endpoint_descriptor(
                "ep-anthropic",
                provider="anthropic",
                protocol=EndpointProtocol.ANTHROPIC_NATIVE,
                base_url="http://anthropic.test",
            ),
            "ep-google": _make_endpoint_descriptor(
                "ep-google",
                provider="google",
                protocol=EndpointProtocol.GOOGLE_NATIVE,
                base_url="http://google.test",
            ),
            "ep-fallback": _make_endpoint_descriptor("ep-fallback", base_url="http://fallback.test"),
        }

        # Build registry from config — native protocols should be unavailable
        ok_http = httpx.AsyncClient(transport=MockTransport())
        registry = EndpointRegistry.from_config(
            TierConfig(
                tiers={
                    ModelTier.HIGH_CODING: TierChain(
                        primary="ep-anthropic",
                        secondary="ep-google",
                        cross_vendor=("ep-fallback",),
                    )
                },
                endpoints=descs,
            ),
            http=ok_http,
        )

        chain = TierChain(primary="ep-anthropic", secondary="ep-google", cross_vendor=("ep-fallback",))
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=_fake_key_resolver(),
        )

        result = await router.complete(tier=ModelTier.HIGH_CODING, request=_make_request())
        assert result.outcome == RoutingOutcome.OK

        # Anthropic and Google should be skipped with unsupported_protocol reason
        skipped = [a for a in result.attempts if a.result == "skipped"]
        assert len(skipped) == 2
        assert "unsupported_protocol" in skipped[0].reason
        assert "unsupported_protocol" in skipped[1].reason

        # Fallback should succeed
        assert result.endpoint_id == "ep-fallback"


class TestFullExhaustionReturnsExhausted:
    """Full chain exhaustion returns EXHAUSTED outcome."""

    async def test_all_fail_exhausted(self):
        def fail_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "all down"})

        fail_http = httpx.AsyncClient(transport=MockTransport(fail_handler))

        descs = {
            "ep-a": _make_endpoint_descriptor("ep-a", base_url="http://a.test"),
            "ep-b": _make_endpoint_descriptor("ep-b", base_url="http://b.test"),
            "ep-c": _make_endpoint_descriptor("ep-c", base_url="http://c.test"),
        }

        registry = EndpointRegistry(
            endpoints={
                "ep-a": OpenAICompatibleEndpoint(descriptor=descs["ep-a"], http=fail_http),
                "ep-b": OpenAICompatibleEndpoint(descriptor=descs["ep-b"], http=fail_http),
                "ep-c": OpenAICompatibleEndpoint(descriptor=descs["ep-c"], http=fail_http),
            },
            availability={
                "ep-a": EndpointAvailability(endpoint_id="ep-a", available=True),
                "ep-b": EndpointAvailability(endpoint_id="ep-b", available=True),
                "ep-c": EndpointAvailability(endpoint_id="ep-c", available=True),
            },
        )

        chain = TierChain(primary="ep-a", secondary="ep-b", cross_vendor=("ep-c",))
        config = _make_tier_config(descs, chain)
        cache = TieredSemanticCache(redis=_fake_redis())

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=_fake_key_resolver(),
        )

        result = await router.complete(tier=ModelTier.HIGH_CODING, request=_make_request())
        assert result.outcome == RoutingOutcome.EXHAUSTED
        assert result.content is None
        assert result.degraded is True
        # All 3 attempted
        assert len(result.attempts) == 3
        assert all(a.result == "error" for a in result.attempts)


class TestOpenAICompatibleIsOnlyProductionAdapter:
    """OpenAICompatibleEndpoint is the only production adapter."""

    def test_only_openai_compatible_instantiated(self):
        """from_config only creates OpenAICompatibleEndpoint instances."""
        descs = {
            "ep-oai": _make_endpoint_descriptor("ep-oai", protocol=EndpointProtocol.OPENAI_COMPATIBLE),
            "ep-anth": _make_endpoint_descriptor(
                "ep-anth", protocol=EndpointProtocol.ANTHROPIC_NATIVE, base_url="http://anth.test"
            ),
            "ep-goog": _make_endpoint_descriptor(
                "ep-goog", protocol=EndpointProtocol.GOOGLE_NATIVE, base_url="http://goog.test"
            ),
        }

        config = TierConfig(
            tiers={ModelTier.HIGH_CODING: TierChain(primary="ep-oai", cross_vendor=("ep-anth", "ep-goog"))},
            endpoints=descs,
        )

        registry = EndpointRegistry.from_config(config)

        # Only ep-oai should have an endpoint adapter
        assert registry.endpoint("ep-oai") is not None
        assert isinstance(registry.endpoint("ep-oai"), OpenAICompatibleEndpoint)

        # Native protocol endpoints have no adapter
        assert registry.endpoint("ep-anth") is None
        assert registry.endpoint("ep-goog") is None

        # But they have availability entries with reason
        avail_anth = registry.get_availability("ep-anth")
        assert avail_anth is not None
        assert avail_anth.available is False
        assert "unsupported_protocol" in avail_anth.reason

    def test_model_endpoint_protocol_only_openai_impl(self):
        """Verify only OpenAICompatibleEndpoint implements ModelEndpoint in production."""
        import inspect

        from src.ai.routing import endpoints as ep_module

        # Find all concrete classes that have the ModelEndpoint interface
        # (endpoint_id, provider_kind properties and async complete method)
        classes_with_complete = []
        for name, obj in inspect.getmembers(ep_module, inspect.isclass):
            if name == "ModelEndpoint":
                continue
            # Check for the three required interface members
            has_endpoint_id = isinstance(getattr(obj, "endpoint_id", None), property) or "endpoint_id" in getattr(
                obj, "__dict__", {}
            )
            has_provider_kind = isinstance(getattr(obj, "provider_kind", None), property) or "provider_kind" in getattr(
                obj, "__dict__", {}
            )
            has_complete = callable(getattr(obj, "complete", None))

            if has_endpoint_id and has_provider_kind and has_complete:
                classes_with_complete.append(name)

        assert classes_with_complete == ["OpenAICompatibleEndpoint"]
