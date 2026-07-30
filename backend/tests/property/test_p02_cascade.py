# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test P-02: Cascade termination guarantees.

Proves:
- Terminates within |chain| iterations
- At-most-once ordered concrete invocation
- Unsupported endpoints are skipped, never invoked
- Provider errors are swallowed (no propagation)
- Result is OK or EXHAUSTED only
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, create_autospec

from hypothesis import given, settings
from hypothesis import strategies as st
from src.ai.routing.cache import TieredSemanticCache
from src.ai.routing.endpoints import (
    CompletionRequest,
    CompletionResponse,
    EndpointAvailability,
    EndpointRegistry,
    ModelEndpoint,
    OpenAICompatibleEndpoint,
)
from src.ai.routing.router import ModelRouter, RoutingOutcome, RoutingResult
from src.ai.routing.tiers import (
    EndpointDescriptor,
    EndpointProtocol,
    ModelTier,
    TierChain,
    TierConfig,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Endpoint behavior: True = success, False = failure, None = unsupported protocol
endpoint_behavior_st = st.sampled_from([True, False, None])

# Generate a chain of 1-6 endpoints with behaviors
chain_st = st.lists(
    st.tuples(
        st.text(min_size=3, max_size=10, alphabet="abcdefghijklmnop"),
        endpoint_behavior_st,
    ),
    min_size=1,
    max_size=6,
).filter(lambda items: len({name for name, _ in items}) == len(items))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_router_from_chain(
    chain_spec: list[tuple[str, bool | None]],
) -> tuple[ModelRouter, dict[str, int]]:
    """Build a router from a chain spec. Returns (router, invocation_counts)."""
    invocation_counts: dict[str, int] = {}
    endpoints: dict[str, ModelEndpoint] = {}
    availability: dict[str, EndpointAvailability] = {}
    descriptors: dict[str, EndpointDescriptor] = {}
    chain_ids: list[str] = []

    for name, behavior in chain_spec:
        eid = f"ep-{name}"
        chain_ids.append(eid)
        invocation_counts[eid] = 0

        if behavior is None:
            # Unsupported protocol â€” mark unavailable, no endpoint adapter
            protocol = EndpointProtocol.ANTHROPIC_NATIVE
            availability[eid] = EndpointAvailability(
                endpoint_id=eid, available=False, reason="unsupported_protocol_phase_0"
            )
        else:
            protocol = EndpointProtocol.OPENAI_COMPATIBLE
            availability[eid] = EndpointAvailability(endpoint_id=eid, available=True)

            # Create autospec'd endpoint. `spec=` alone constrains attribute NAMES
            # but not child SIGNATURES, so `complete` could be called with any
            # arguments at all - the D-23 hole. autospec closes it; spec_set makes
            # assigning over a child raise. `endpoint_id`/`provider_kind` are
            # class-level properties, so they are part of the spec and remain
            # settable here.
            mock_ep = create_autospec(OpenAICompatibleEndpoint, spec_set=True, instance=True)
            mock_ep.endpoint_id = eid
            mock_ep.provider_kind = "test"

            if behavior:
                # Success
                async def _success(req, *, api_key=None, _eid=eid):
                    invocation_counts[_eid] += 1
                    return CompletionResponse(content=f"ok from {_eid}", model="test")

                mock_ep.complete = _success
            else:
                # Failure
                async def _fail(req, *, api_key=None, _eid=eid):
                    invocation_counts[_eid] += 1
                    raise RuntimeError(f"provider error from {_eid}")

                mock_ep.complete = _fail

            endpoints[eid] = mock_ep

        descriptors[eid] = EndpointDescriptor(
            id=eid,
            provider="test",
            model="test-model",
            protocol=protocol,
            base_url="http://test.local",
            key_ref=None,
        )

    # Build tier chain: first is primary, second is secondary, rest are cross_vendor
    primary = chain_ids[0]
    secondary = chain_ids[1] if len(chain_ids) > 1 else None
    cross_vendor = tuple(chain_ids[2:]) if len(chain_ids) > 2 else ()

    tier_chain = TierChain(primary=primary, secondary=secondary, cross_vendor=cross_vendor)
    config = TierConfig(
        tiers={ModelTier.HIGH_CODING: tier_chain},
        endpoints=descriptors,
    )

    registry = EndpointRegistry(endpoints=endpoints, availability=availability)

    # Cache always misses
    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(return_value=None)
    redis_mock.set = AsyncMock(return_value=True)
    cache = TieredSemanticCache(redis=redis_mock)

    # No key resolver needed (key_ref is None)
    key_resolver = MagicMock()
    key_resolver.resolve = MagicMock(return_value=None)

    router = ModelRouter(
        tier_config=config,
        registry=registry,
        cache=cache,
        breakers={},
        key_resolver=key_resolver,
    )

    return router, invocation_counts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCascadeTermination:
    """Property test: cascade always terminates within |chain| iterations."""

    @given(chain=chain_st)
    @settings(max_examples=100, deadline=None)
    async def test_terminates_within_chain_length(self, chain):
        """Cascade terminates with at most |chain| attempts."""
        router, counts = _build_router_from_chain(chain)
        request = CompletionRequest(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
        )

        result = await router.complete(tier=ModelTier.HIGH_CODING, request=request)

        # Must terminate (we got here) with at most |chain| attempts
        assert len(result.attempts) <= len(chain)

    @given(chain=chain_st)
    @settings(max_examples=100, deadline=None)
    async def test_result_is_ok_or_exhausted(self, chain):
        """Result is always OK or EXHAUSTED, nothing else."""
        router, counts = _build_router_from_chain(chain)
        request = CompletionRequest(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
        )

        result = await router.complete(tier=ModelTier.HIGH_CODING, request=request)
        assert result.outcome in (RoutingOutcome.OK, RoutingOutcome.EXHAUSTED)


class TestAtMostOnceOrderedInvocation:
    """Each endpoint invoked at most once, in chain order."""

    @given(chain=chain_st)
    @settings(max_examples=100, deadline=None)
    async def test_at_most_once(self, chain):
        """Each endpoint invoked at most once."""
        router, counts = _build_router_from_chain(chain)
        request = CompletionRequest(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
        )

        await router.complete(tier=ModelTier.HIGH_CODING, request=request)

        for eid, count in counts.items():
            assert count <= 1, f"{eid} invoked {count} times"

    @given(chain=chain_st)
    @settings(max_examples=100, deadline=None)
    async def test_ordered_invocation(self, chain):
        """Invocations follow chain order."""
        router, counts = _build_router_from_chain(chain)
        request = CompletionRequest(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
        )

        result = await router.complete(tier=ModelTier.HIGH_CODING, request=request)

        # Extract invoked endpoint IDs in order from attempts
        invoked_ids = [a.endpoint_id for a in result.attempts]
        # Build expected chain order
        chain_order = [f"ep-{name}" for name, _ in chain]

        # invoked_ids must be a subsequence of chain_order
        chain_iter = iter(chain_order)
        for inv_id in invoked_ids:
            found = False
            for c_id in chain_iter:
                if c_id == inv_id:
                    found = True
                    break
            assert found, f"{inv_id} not found in order in chain"


class TestUnsupportedEndpointsNeverInvoked:
    """Unsupported endpoints are skipped, never invoked."""

    @given(chain=chain_st)
    @settings(max_examples=100, deadline=None)
    async def test_unsupported_never_invoked(self, chain):
        """Endpoints with unsupported protocol are never invoked."""
        router, counts = _build_router_from_chain(chain)
        request = CompletionRequest(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
        )

        _result = await router.complete(tier=ModelTier.HIGH_CODING, request=request)

        # Unsupported endpoints (behavior=None) should have 0 invocations
        for name, behavior in chain:
            if behavior is None:
                eid = f"ep-{name}"
                assert counts[eid] == 0, f"Unsupported {eid} was invoked"


class TestProviderErrorsSwallowed:
    """Provider errors are swallowed, not propagated to caller."""

    @given(chain=chain_st)
    @settings(max_examples=100, deadline=None)
    async def test_errors_swallowed(self, chain):
        """No exception propagates from the cascade â€” always returns a result."""
        router, counts = _build_router_from_chain(chain)
        request = CompletionRequest(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
        )

        # This must not raise
        result = await router.complete(tier=ModelTier.HIGH_CODING, request=request)
        assert isinstance(result, RoutingResult)
        assert result.outcome in (RoutingOutcome.OK, RoutingOutcome.EXHAUSTED)
