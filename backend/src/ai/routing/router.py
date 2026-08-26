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

from src.secrets.redaction import RedactedPrompt

from .breaker import CircuitBreaker
from .cache import TieredSemanticCache
from .endpoints import (
    CompletionRequest,
    CompletionResponse,
    EndpointRegistry,
    MalformedResponseError,
    StreamingModelEndpoint,
    TokenSink,
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
    #: Set only when an endpoint answered, so a caller can record real token counts.
    #:
    #: `None` on a cache hit, and that distinction is the point: a hit cost no tokens, and
    #: reporting the ORIGINAL call's usage against it would inflate NFR-04's cost evidence every
    #: time the cache did its job.
    usage: dict[str, int] | None = None
    #: True when the content reached the caller as it was produced rather than in one piece.
    streamed: bool = False


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
        prompt: RedactedPrompt,
        on_token: TokenSink | None = None,
    ) -> RoutingResult:
        """Route a completion request through the tier cascade.

        Returns a RoutingResult with outcome OK or EXHAUSTED.

        `on_token`, when supplied, asks each endpoint for its output AS IT IS PRODUCED. It changes
        the transport and nothing else: the cache lookup, the breaker accounting, the dedup, the
        attempt records and the cached content are all identical, because `complete_streaming`
        returns the same `CompletionResponse` the whole-response path does. An endpoint that does
        not implement `StreamingModelEndpoint` is invoked the ordinary way and the caller simply
        receives no deltas — a provider without server-sent frames must be a slower stream, never a
        failed one.

        A CACHE HIT DELIVERS NOTHING TO `on_token`, deliberately. The hit's content is returned in
        full, and it is the CALLER's business to decide how to present it; replaying it through the
        sink here would make a cache hit indistinguishable from a provider call at the point where
        `served_from` is decided, which is the distinction this whole path exists to record.
        """
        attempts: list[Attempt] = []

        # 1. Check semantic cache first
        cache_hit = await self._cache.lookup(
            model=request.model,
            prompt=prompt,
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
            credential: str | None = None
            if descriptor and descriptor.key_ref:
                secret = self._key_resolver.resolve(descriptor.key_ref)
                if secret is not None:
                    credential = secret.get_secret_value()

            # Invoke endpoint
            start = time.perf_counter()
            try:
                streaming = on_token is not None and isinstance(endpoint, StreamingModelEndpoint)
                if streaming:
                    assert on_token is not None  # narrowed by `streaming`
                    response: CompletionResponse = await endpoint.complete_streaming(
                        request, credential=credential, on_token=on_token
                    )
                else:
                    response = await endpoint.complete(request, credential=credential)
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
                    prompt=prompt,
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
                    usage=dict(response.usage),
                    streamed=streaming,
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
