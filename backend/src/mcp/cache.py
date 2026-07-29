# SPDX-License-Identifier: FSL-1.1-ALv2
"""Redis-authoritative tool-list cache for the MCP gateway (Design §11.4, P-06).

Redis is the *sole* runtime expiry authority across gateway replicas. Runtime
state stores the encoded tool list under a `SET ... PX` expiry and never stores a
process-monotonic absolute timestamp, because timestamps minted in different
processes are not comparable. A monotonic clock exists only in the pure reference
model used by `tests/property/test_p06_ttl_cache.py`.

The cache exchanges `list[dict]` with its caller and owns JSON encoding itself.
An earlier revision took a pre-encoded `str` while the gateway passed a list,
which no test caught because the gateway was only ever composed with fakes.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class RedisLike(Protocol):
    """Minimal async Redis interface for testability."""

    async def set(self, name: str, value: str, px: int | None = None) -> Any: ...
    async def get(self, name: str) -> str | None: ...
    async def pttl(self, name: str) -> int: ...


class TtlToolCache:
    """Tool-list cache keyed by MCP server name, with Redis-owned expiry.

    - `SET PX min(server_ttl_ms, max_ttl_ms)`; a missing or non-positive server
      TTL creates no key at all.
    - A value is returned only while Redis reports `PTTL > 0`.
    - Redis failure degrades to a cache miss; it never turns an expired value
      into a hit and never installs a local fallback expiry.
    """

    KEY_PREFIX = "mcp:tools:"

    def __init__(self, redis: RedisLike, *, max_ttl_ms: int = 60_000) -> None:
        self._redis = redis
        self._max_ttl_ms = max_ttl_ms

    def key_for(self, server: str) -> str:
        """Public so tests can assert on the stored key without guessing."""
        return f"{self.KEY_PREFIX}{server}"

    def _effective_ttl_ms(self, server_ttl_ms: int | None) -> int:
        """Clamp a server-declared ttlMs; 0 means "do not cache"."""
        if server_ttl_ms is None or server_ttl_ms <= 0:
            return 0
        return min(server_ttl_ms, self._max_ttl_ms)

    async def put(
        self,
        server: str,
        tools: list[dict[str, Any]],
        server_ttl_ms: int | None,
    ) -> bool:
        """Store a tool list under `min(server_ttl_ms, max_ttl_ms)`.

        Returns True when a key was written, False when caching was skipped
        (absent or non-positive TTL) or Redis failed.
        """
        ttl_ms = self._effective_ttl_ms(server_ttl_ms)
        if ttl_ms <= 0:
            return False

        try:
            await self._redis.set(self.key_for(server), json.dumps(tools), px=ttl_ms)
            return True
        except Exception:
            logger.warning("redis SET failed for server=%s; treating as no-cache", server)
            return False

    async def get(self, server: str) -> list[dict[str, Any]] | None:
        """Return the cached tool list only while Redis reports `PTTL > 0`."""
        key = self.key_for(server)
        try:
            pttl = await self._redis.pttl(key)
            if pttl <= 0:
                return None
            raw = await self._redis.get(key)
        except Exception:
            logger.warning("redis GET failed for server=%s; degrading to a cache miss", server)
            return None

        if raw is None:
            return None
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("cached tool list for server=%s is not valid JSON; treating as a miss", server)
            return None
        return decoded if isinstance(decoded, list) else None
