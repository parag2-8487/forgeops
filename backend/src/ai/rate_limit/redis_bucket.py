# SPDX-License-Identifier: FSL-1.1-ALv2
"""Redis/Lua atomic token bucket rate limiter (Design §13.6).

- Lua script ensures atomic refill + consume in a single Redis round-trip.
- Fail-closed: Redis failure → 503 (ServiceUnavailable).
- Exhaustion → 429 with Retry-After seconds.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Lua script: atomic token bucket refill + consume.
# KEYS[1] = bucket key
# ARGV[1] = capacity (max tokens)
# ARGV[2] = refill_rate (tokens per second)
# ARGV[3] = tokens_to_consume (usually 1)
#
# The clock is read from Redis TIME inside the script, never supplied by the
# caller, so the bucket is authoritative across replicas (design §14.1).
#
# Returns: [allowed(0|1), remaining, retry_after_ms]
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local consume = tonumber(ARGV[3])

-- Redis is the single time authority. A client-supplied timestamp would let
-- replicas with skewed clocks refill the same bucket at different rates, and a
-- caller able to influence the app clock could refill it arbitrarily fast.
-- TIME returns {seconds, microseconds} and is fixed for the duration of one EVAL.
local t = redis.call('TIME')
local now = tonumber(t[1]) + (tonumber(t[2]) / 1000000)

local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

-- Refill tokens based on elapsed time
local elapsed = math.max(0, now - last_refill)
local refill = elapsed * refill_rate
tokens = math.min(capacity, tokens + refill)
last_refill = now

-- Attempt consume
if tokens >= consume then
    tokens = tokens - consume
    redis.call('HMSET', key, 'tokens', tostring(tokens), 'last_refill', tostring(last_refill))
    redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 60)
    return {1, math.floor(tokens), 0}
else
    -- Denied: calculate retry_after in milliseconds
    local deficit = consume - tokens
    local retry_after_ms = math.ceil((deficit / refill_rate) * 1000)
    redis.call('HMSET', key, 'tokens', tostring(tokens), 'last_refill', tostring(last_refill))
    redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 60)
    return {0, math.floor(tokens), retry_after_ms}
end
"""


@dataclass(frozen=True)
class RateLimitDecision:
    """Result of a rate limit check."""

    allowed: bool
    remaining: int
    retry_after_seconds: float | None = None


@runtime_checkable
class AsyncRedisLike(Protocol):
    """Minimal async Redis interface for testability."""

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...


class RedisTokenBucketLimiter:
    """Atomic token bucket rate limiter using Redis + Lua.

    Fail-closed: if Redis is unreachable, deny with 503 semantics.
    """

    def __init__(
        self,
        *,
        redis: AsyncRedisLike,
        capacity: int = 100,
        refill_rate: float = 10.0,
        key_prefix: str = "ai:ratelimit:",
        clock: Any | None = None,
    ) -> None:
        self._redis = redis
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._prefix = key_prefix
        # Kept for the pure reference model in tests only. The runtime bucket
        # reads Redis TIME inside the Lua script; this clock never reaches it.
        self._clock = clock or time.time

    async def check(self, bucket_id: str, *, tokens: int = 1) -> RateLimitDecision:
        """Check rate limit for the given bucket.

        Returns:
            RateLimitDecision with allowed=True/False.

        Raises:
            RateLimitServiceError on Redis failure (fail-closed → 503).
        """
        key = f"{self._prefix}{bucket_id}"

        try:
            result = await self._redis.eval(
                TOKEN_BUCKET_LUA,
                1,
                key,
                str(self._capacity),
                str(self._refill_rate),
                str(tokens),
            )
        except Exception as exc:
            logger.error("Redis rate limit failure (fail-closed): %s", exc)
            raise RateLimitServiceError("Rate limit service unavailable (fail-closed → 503)") from exc

        allowed = bool(result[0])
        remaining = int(result[1])
        retry_after_ms = int(result[2])

        return RateLimitDecision(
            allowed=allowed,
            remaining=remaining,
            retry_after_seconds=retry_after_ms / 1000.0 if not allowed else None,
        )


class RateLimitServiceError(Exception):
    """Raised when the rate limit backend is unavailable (fail-closed → 503)."""

    pass
