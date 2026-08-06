# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test P-03: Zero invocation of skipped endpoints.

Proves:
- Open breaker → skipped_open_breaker with zero invocations
- Unsupported protocol → skipped_unavailable with zero invocations
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st
from src.ai.routing.breaker import BreakerState, CircuitBreaker
from src.ai.routing.cache import TieredSemanticCache
from src.ai.routing.endpoints import (
    CompletionRequest,
    CompletionResponse,
    EndpointAvailability,
    EndpointRegistry,
)
from src.ai.routing.router import ModelRouter, RoutingOutcome
from src.ai.routing.tiers import (
    EndpointDescriptor,
    EndpointProtocol,
    ModelTier,
    TierChain,
    TierConfig,
)
from src.secrets.redaction import create_redacted_chunk

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Number of endpoints to skip before the final working one
num_skipped_st = st.integers(min_value=1, max_value=5)


# ---------------------------------------------------------------------------
# Tests: Open breaker → zero invocations
# ---------------------------------------------------------------------------


class TestOpenBreakerZeroInvocations:
    """Open circuit breaker causes skip with zero invocations."""

    @given(num_open=num_skipped_st)
    @settings(max_examples=50, deadline=None)
    async def test_open_breaker_skipped(self, num_open: int):
        """Endpoints with open breakers are skipped with zero invocations."""
        invocation_counts: dict[str, int] = {}
        endpoints: dict[str, Any] = {}
        availability: dict[str, EndpointAvailability] = {}
        descriptors: dict[str, EndpointDescriptor] = {}
        breakers: dict[str, CircuitBreaker] = {}
        chain_ids: list[str] = []

        # Create `num_open` endpoints with open breakers
        for i in range(num_open):
            eid = f"ep-open-{i}"
            chain_ids.append(eid)
            invocation_counts[eid] = 0

            descriptors[eid] = EndpointDescriptor(
                id=eid,
                provider="test",
                model="m",
                protocol=EndpointProtocol.OPENAI_COMPATIBLE,
                base_url="http://test.local",
                key_ref=None,
            )
            availability[eid] = EndpointAvailability(endpoint_id=eid, available=True)

            # Create mock endpoint that counts invocations
            mock_ep = AsyncMock()
            mock_ep.endpoint_id = eid
            mock_ep.provider_kind = "test"

            async def _invoke(req, *, api_key=None, _eid=eid):
                invocation_counts[_eid] += 1
                return CompletionResponse(content="should not reach", model="m")

            mock_ep.complete = _invoke
            endpoints[eid] = mock_ep

            # Create breaker that is OPEN
            t = 0.0
            breaker = CircuitBreaker(
                failure_threshold=1,
                failure_window_seconds=10.0,
                cooldown_seconds=9999.0,  # Never auto-recover in test
                clock=lambda t=t: t,
            )
            breaker.record_failure()  # Trip it
            assert breaker.state() == BreakerState.OPEN
            breakers[eid] = breaker

        # Add one working endpoint at the end
        final_eid = "ep-final"
        chain_ids.append(final_eid)
        invocation_counts[final_eid] = 0
        descriptors[final_eid] = EndpointDescriptor(
            id=final_eid,
            provider="test",
            model="m",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="http://test.local",
            key_ref=None,
        )
        availability[final_eid] = EndpointAvailability(endpoint_id=final_eid, available=True)

        mock_final = AsyncMock()
        mock_final.endpoint_id = final_eid
        mock_final.provider_kind = "test"

        async def _final_invoke(req, *, api_key=None):
            invocation_counts[final_eid] += 1
            return CompletionResponse(content="final ok", model="m")

        mock_final.complete = _final_invoke
        endpoints[final_eid] = mock_final

        # Build router
        primary = chain_ids[0]
        secondary = chain_ids[1] if len(chain_ids) > 1 else None
        cross_vendor = tuple(chain_ids[2:]) if len(chain_ids) > 2 else ()

        tier_chain = TierChain(primary=primary, secondary=secondary, cross_vendor=cross_vendor)
        config = TierConfig(tiers={ModelTier.HIGH_CODING: tier_chain}, endpoints=descriptors)
        registry = EndpointRegistry(endpoints=endpoints, availability=availability)

        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.set = AsyncMock(return_value=True)
        cache = TieredSemanticCache(redis=redis_mock)

        key_resolver = MagicMock()
        key_resolver.resolve = MagicMock(return_value=None)

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers=breakers,
            key_resolver=key_resolver,
        )

        request = CompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])
        result = await router.complete(tier=ModelTier.HIGH_CODING, request=request, prompt=create_redacted_chunk("foo"))

        # Verify open-breaker endpoints have zero invocations
        for i in range(num_open):
            eid = f"ep-open-{i}"
            assert invocation_counts[eid] == 0, f"{eid} was invoked despite open breaker"

        # Verify they are marked as skipped with breaker reason
        skipped = [a for a in result.attempts if a.result == "skipped"]
        for s in skipped:
            assert "circuit_breaker_open" in (s.reason or "")

        # Final endpoint was invoked
        assert invocation_counts[final_eid] == 1
        assert result.outcome == RoutingOutcome.OK


# ---------------------------------------------------------------------------
# Tests: Unsupported protocol → zero invocations
# ---------------------------------------------------------------------------


class TestUnsupportedProtocolZeroInvocations:
    """Unsupported protocol endpoints are skipped with zero invocations."""

    @given(num_unsupported=num_skipped_st)
    @settings(max_examples=50, deadline=None)
    async def test_unsupported_protocol_skipped(self, num_unsupported: int):
        """Endpoints with unsupported protocols are never invoked."""
        invocation_counts: dict[str, int] = {}
        endpoints: dict[str, Any] = {}
        availability: dict[str, EndpointAvailability] = {}
        descriptors: dict[str, EndpointDescriptor] = {}
        chain_ids: list[str] = []

        # Create unsupported endpoints (they should NOT be in the endpoints dict)
        for i in range(num_unsupported):
            eid = f"ep-native-{i}"
            chain_ids.append(eid)
            invocation_counts[eid] = 0

            protocol = EndpointProtocol.ANTHROPIC_NATIVE if i % 2 == 0 else EndpointProtocol.GOOGLE_NATIVE
            descriptors[eid] = EndpointDescriptor(
                id=eid,
                provider="test",
                model="m",
                protocol=protocol,
                base_url="http://test.local",
                key_ref=None,
            )
            # Marked unavailable
            availability[eid] = EndpointAvailability(
                endpoint_id=eid, available=False, reason="unsupported_protocol_phase_0"
            )
            # No endpoint adapter registered (as from_config would do)

        # Add one working endpoint at the end
        final_eid = "ep-final-ok"
        chain_ids.append(final_eid)
        invocation_counts[final_eid] = 0
        descriptors[final_eid] = EndpointDescriptor(
            id=final_eid,
            provider="test",
            model="m",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="http://test.local",
            key_ref=None,
        )
        availability[final_eid] = EndpointAvailability(endpoint_id=final_eid, available=True)

        mock_final = AsyncMock()
        mock_final.endpoint_id = final_eid
        mock_final.provider_kind = "test"

        async def _final_invoke(req, *, api_key=None):
            invocation_counts[final_eid] += 1
            return CompletionResponse(content="final ok", model="m")

        mock_final.complete = _final_invoke
        endpoints[final_eid] = mock_final

        # Build router
        primary = chain_ids[0]
        secondary = chain_ids[1] if len(chain_ids) > 1 else None
        cross_vendor = tuple(chain_ids[2:]) if len(chain_ids) > 2 else ()

        tier_chain = TierChain(primary=primary, secondary=secondary, cross_vendor=cross_vendor)
        config = TierConfig(tiers={ModelTier.HIGH_CODING: tier_chain}, endpoints=descriptors)
        registry = EndpointRegistry(endpoints=endpoints, availability=availability)

        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.set = AsyncMock(return_value=True)
        cache = TieredSemanticCache(redis=redis_mock)

        key_resolver = MagicMock()
        key_resolver.resolve = MagicMock(return_value=None)

        router = ModelRouter(
            tier_config=config,
            registry=registry,
            cache=cache,
            breakers={},
            key_resolver=key_resolver,
        )

        request = CompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])
        result = await router.complete(tier=ModelTier.HIGH_CODING, request=request, prompt=create_redacted_chunk("foo"))

        # Verify unsupported endpoints have zero invocations
        for i in range(num_unsupported):
            eid = f"ep-native-{i}"
            assert invocation_counts[eid] == 0, f"{eid} was invoked despite unsupported protocol"

        # Verify skipped with appropriate reason
        skipped = [a for a in result.attempts if a.result == "skipped"]
        for s in skipped:
            assert "unsupported_protocol" in (s.reason or "") or "unavailable" in (s.reason or "")

        # Final endpoint was invoked
        assert invocation_counts[final_eid] == 1
        assert result.outcome == RoutingOutcome.OK
