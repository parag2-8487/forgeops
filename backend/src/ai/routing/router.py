# SPDX-License-Identifier: FSL-1.1-ALv2
"""Concrete fallback model router (Design §13.7).

Cascades through a deduplicated endpoint chain per tier, skipping open breakers
and unavailable protocols. Never generates fake/template responses — ends with
EXHAUSTED if all endpoints fail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from .breaker import CircuitBreaker
from .cache import TieredSemanticCache
from .endpoints import (
    CompletionRequest,
    CompletionResponse,
    EndpointRegistry,
    MalformedResponseError,
)
from .keys import KeyResolver
from .tiers import ModelTier, TierConfig


class RoutingOutcome(StrEnum):
    OK = "ok"
    EXHAUSTED = "exhausted"


@dataclass
class Attempt:
    """Records a single endpoint invocation attempt."""

    endpoint_id: str
    # One of: "success", "error", "timeout", "malformed_response",
    # "skipped_open_breaker", "skipped_unavailable" (Appendix B P-02/P-03).
    result: str
    latency_ms: float = 0.0
    reason: str | None = None


@dataclass
class RoutingResult:
    """Full result of a routing attempt through the cascade."""

    outcome: RoutingOutcome
    endpoint_id: str | None = None
    content: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    served_from: str | None = None  # "L1_exact", "endpoint", etc.
    degraded: bool = False
    staleness_seconds: float = 0.0


class ModelRouter:
    """Concrete fallback router with cache, breaker, and cascade logic.

    - Checks semantic cache first (L1 exact match).
    - Builds deduplicated endpoint chain from tier config.
    - Skips open breakers and unavailable protocols (with reasons).
    - Invokes only registry-provided endpoints, at most once each.
    - Records breaker success/failure after each attempt.
    - Ends with EXHAUSTED (never fakes a template response).
    """

    def __init__(
        self,
        *,
        tier_config: TierConfig,
        registry: EndpointRegistry,
        cache: TieredSemanticCache,
        breakers: dict[str, CircuitBreaker],
        key_resolver: KeyResolver,
    ) -> None:
        self._tier_config = tier_config
        self._registry = registry
        self._cache = cache
        self._breakers = breakers
        self._key_resolver = key_resolver

    async def complete(
        self,
        *,
        tier: ModelTier,
        request: CompletionRequest,
    ) -> RoutingResult:
        """Route a completion request through the tier cascade.

        Returns a RoutingResult with outcome OK or EXHAUSTED.
        """
        attempts: list[Attempt] = []

        # 1. Check semantic cache first
        cache_hit = await self._cache.lookup(
            model=request.model,
            messages=request.messages,
            params={"temperature": request.temperature, "max_tokens": request.max_tokens},
        )
        if cache_hit is not None:
            return RoutingResult(
                outcome=RoutingOutcome.OK,
                endpoint_id=None,
                content=cache_hit.content,
                attempts=attempts,
                served_from=cache_hit.served_from,
                degraded=cache_hit.degraded,
                staleness_seconds=cache_hit.staleness_seconds,
            )

        # 2. Build deduplicated chain from tier config
        tier_chain = self._tier_config.tiers.get(tier)
        if tier_chain is None:
            return RoutingResult(
                outcome=RoutingOutcome.EXHAUSTED,
                attempts=attempts,
            )

        endpoint_ids = tier_chain.ordered_ids()

        # 3. Iterate through chain, invoke at most once per endpoint
        invoked: set[str] = set()

        for eid in endpoint_ids:
            if eid in invoked:
                continue
            invoked.add(eid)

            # Check breaker
            breaker = self._breakers.get(eid)
            if breaker and not breaker.allows():
                attempts.append(
                    Attempt(
                        endpoint_id=eid,
                        result="skipped",
                        reason="circuit_breaker_open",
                    )
                )
                continue

            # Check availability from registry
            availability = self._registry.get_availability(eid)
            if availability and not availability.available:
                attempts.append(
                    Attempt(
                        endpoint_id=eid,
                        result="skipped",
                        reason=availability.reason or "unavailable",
                    )
                )
                continue

            # Get the endpoint adapter
            endpoint = self._registry.endpoint(eid)
            if endpoint is None:
                attempts.append(
                    Attempt(
                        endpoint_id=eid,
                        result="skipped",
                        reason="not_registered",
                    )
                )
                continue

            # Resolve API key
            descriptor = self._tier_config.endpoints.get(eid)
            api_key: str | None = None
            if descriptor and descriptor.key_ref:
                secret = self._key_resolver.resolve(descriptor.key_ref)
                if secret is not None:
                    api_key = secret.get_secret_value()

            # Invoke endpoint
            start = time.perf_counter()
            try:
                response: CompletionResponse = await endpoint.complete(request, api_key=api_key)
                latency_ms = (time.perf_counter() - start) * 1000

                # Record success on breaker
                if breaker:
                    breaker.record_success()

                attempts.append(
                    Attempt(
                        endpoint_id=eid,
                        result="success",
                        latency_ms=latency_ms,
                    )
                )

                # Store in cache for future hits
                await self._cache.store(
                    model=request.model,
                    messages=request.messages,
                    params={"temperature": request.temperature, "max_tokens": request.max_tokens},
                    content=response.content,
                )

                return RoutingResult(
                    outcome=RoutingOutcome.OK,
                    endpoint_id=eid,
                    content=response.content,
                    attempts=attempts,
                    served_from="endpoint",
                    degraded=len(attempts) > 1,
                    staleness_seconds=0.0,
                )

            except Exception as exc:
                latency_ms = (time.perf_counter() - start) * 1000

                # Record failure on breaker
                if breaker:
                    breaker.record_failure()

                # Appendix B P-02 distinguishes timeout and malformed_response from
                # a generic error, because the three say different things about the
                # endpoint: a timeout may be transient load, a malformed body means
                # the endpoint is not speaking the protocol we validated against.
                if isinstance(exc, MalformedResponseError):
                    attempt_result = "malformed_response"
                elif isinstance(exc, httpx.TimeoutException):
                    attempt_result = "timeout"
                else:
                    attempt_result = "error"

                attempts.append(
                    Attempt(
                        endpoint_id=eid,
                        result=attempt_result,
                        latency_ms=latency_ms,
                        # Truncated: provider error text can carry echoed
                        # credentials or prompt fragments (§14.1).
                        reason=str(exc)[:200],
                    )
                )
                continue

        # 4. All endpoints exhausted — never fake a response
        return RoutingResult(
            outcome=RoutingOutcome.EXHAUSTED,
            attempts=attempts,
            degraded=True,
        )
