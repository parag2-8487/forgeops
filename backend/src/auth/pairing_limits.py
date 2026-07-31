# SPDX-License-Identifier: FSL-1.1-ALv2
"""The two exchange rate limits §14.6's arithmetic depends on (§10.3, §14.6, Appendix A.1).

Why two buckets and not one
--------------------------
§14.6 does the arithmetic twice, for two different attackers:

* **One IP.** 10 attempts per minute × a 5-minute code lifetime = 50 guesses against at most 10
  live codes ⇒ P(success) ≈ 4.7 × 10⁻⁷.
* **Many IPs.** The per-IP bucket says nothing about a distributed attacker, so a **global**
  bucket is what bounds the total. §14.6 sizes it so "total attempts across the window cannot
  exceed 600" ⇒ P ≈ 5.6 × 10⁻⁶.

A single bucket cannot express both: one keyed by IP does not bound the total, and one keyed
globally at 600 would let a single IP spend the whole allowance. So the exchange checks both, in
that order, and the design's two numbers stay two settings.

Why this file wraps `RedisTokenBucketLimiter` instead of importing it
--------------------------------------------------------------------
`RedisTokenBucketLimiter` lives in `src/ai/rate_limit/redis_bucket.py`, and `src.ai` is banned
cross-domain by §2.4's Ruff table — `auth` may not import it, and neither may `core`. Writing a
second limiter would be worse than the ban: two Lua token buckets in one codebase is two places
for the clock authority to be got wrong, and §14.1 is explicit that the bucket's clock must come
from Redis rather than from a caller.

So this module declares the *shape* it needs and takes the bucket as a collaborator. `main.py`
— the composition root, which is exempt because composition is its job — constructs the two
`RedisTokenBucketLimiter` instances and injects them. The existing limiter is reused verbatim,
the ban is honoured, and no domain learns about another.

Why an outage is a refusal
--------------------------
`RedisTokenBucketLimiter.check` raises on Redis failure and the underlying limiter's own comment
calls that fail-closed. This module keeps that direction and converts it to
`PairingUnavailableError` (D-71 → 503 `pairing-unavailable`) rather than letting an unbounded
number of guesses through during an outage. The exchange could not have succeeded anyway: the
single-use consume script is a Redis `EVAL`, so a Redis outage refuses the exchange one way or
another. Making the refusal explicit here is what stops it arriving as a 500.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "GLOBAL_BUCKET_ID",
    "PairingExchangeLimiter",
    "PairingUnavailableError",
    "RateLimitVerdict",
    "TokenBucket",
    "TokenBucketPairingLimiter",
]

#: The one key the global bucket uses. A constant rather than a parameter: a global bucket whose
#: key a caller could vary is not global.
GLOBAL_BUCKET_ID: str = "pair_global"


class PairingUnavailableError(RuntimeError):
    """A dependency the exchange cannot proceed without is unreachable (D-71).

    Distinct from "rate limited". A 429 tells a client to slow down and try again; this says the
    server could not evaluate the limit at all, which is a 503 and must not be reported as the
    former (the D-56 lesson, applied to pairing).
    """


@dataclass(frozen=True, slots=True)
class RateLimitVerdict:
    """Whether an attempt is permitted, and when to retry if not."""

    allowed: bool
    retry_after_seconds: int


@runtime_checkable
class BucketDecision(Protocol):
    """The shape `RedisTokenBucketLimiter.check` returns, named without importing it."""

    allowed: bool
    retry_after_seconds: float | None


@runtime_checkable
class TokenBucket(Protocol):
    """One atomic token bucket, keyed by a caller-supplied id.

    Structurally satisfied by `src.ai.rate_limit.redis_bucket.RedisTokenBucketLimiter`, which is
    the intended implementation and the only one in the codebase. Declared as a Protocol so this
    module does not import a banned package and so a test can substitute a signature-enforcing
    double rather than a `Mock` (§0.4.3).
    """

    async def check(self, bucket_id: str, *, tokens: int = 1) -> Any: ...


class PairingExchangeLimiter(Protocol):
    """The pair of limits `DeviceService.exchange` consults, in §14.6's order."""

    async def check_per_ip(self, client_ip: str) -> RateLimitVerdict: ...

    async def check_global(self) -> RateLimitVerdict: ...


class TokenBucketPairingLimiter:
    """`PairingExchangeLimiter` over two injected token buckets.

    Two collaborators for what §7.8 calls one responsibility, and the split is the responsibility:
    the per-IP and global caps are two different bounds on the same event, sized independently by
    §14.6, and a single bucket cannot carry two capacities.
    """

    def __init__(self, *, per_ip: TokenBucket, global_bucket: TokenBucket) -> None:
        self._per_ip = per_ip
        self._global = global_bucket

    async def check_per_ip(self, client_ip: str) -> RateLimitVerdict:
        """The per-IP cap. An absent or unparseable client address is one bucket, not none.

        A request whose peer address cannot be determined — behind a proxy that strips it, or a
        transport without one — is bucketed under `unknown` rather than exempted. Exempting it
        would make "no client address" the cheapest way to get unlimited attempts.
        """
        return await self._consume(self._per_ip, client_ip.strip() or "unknown")

    async def check_global(self) -> RateLimitVerdict:
        return await self._consume(self._global, GLOBAL_BUCKET_ID)

    @staticmethod
    async def _consume(bucket: TokenBucket, bucket_id: str) -> RateLimitVerdict:
        try:
            decision = await bucket.check(bucket_id)
        except Exception as exc:  # noqa: BLE001 - the limiter's own contract is "raise on outage"
            raise PairingUnavailableError(
                "the pairing exchange rate limiter is unavailable; the exchange is refused "
                "rather than allowed unbounded attempts (§14.6, D-71)"
            ) from exc
        allowed = bool(getattr(decision, "allowed", False))
        raw_retry = getattr(decision, "retry_after_seconds", None)
        # `Retry-After` is an integer number of seconds (RFC 9110 §10.2.3), and the bucket
        # reports fractions. Rounded UP, because rounding a 0.4 s deficit down to 0 would
        # invite an immediate retry that is guaranteed to fail.
        retry = 0 if allowed else max(1, -(-int(float(raw_retry or 1) * 1000) // 1000))
        return RateLimitVerdict(allowed=allowed, retry_after_seconds=retry)
