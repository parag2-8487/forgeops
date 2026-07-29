# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tiered semantic cache — L1 exact-match via Redis (Design §13.4).

L1: SHA-256 of canonical (model + prompt + params) as exact-match key.
Future phases add L2 semantic similarity matching.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class CacheHit:
    """Result returned when a cache lookup succeeds."""

    served_from: str  # e.g. "L1_exact"
    content: str
    degraded: bool = False
    staleness_seconds: float = 0.0


@runtime_checkable
class AsyncRedisLike(Protocol):
    """Minimal async Redis interface for testability."""

    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: str | bytes, *, ex: int | None = None) -> Any: ...


class TieredSemanticCache:
    """L1 exact-match cache backed by Redis.

    Key = SHA-256( canonical JSON of model + messages + params ).
    Value = JSON-encoded completion response content.
    """

    def __init__(
        self,
        *,
        redis: AsyncRedisLike,
        default_ttl_seconds: int = 3600,
        key_prefix: str = "ai:cache:l1:",
    ) -> None:
        self._redis = redis
        self._default_ttl = default_ttl_seconds
        self._prefix = key_prefix

    async def lookup(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any] | None = None,
    ) -> CacheHit | None:
        """Look up a cached response by exact key match."""
        cache_key = self._build_key(model, messages, params)
        raw = await self._redis.get(f"{self._prefix}{cache_key}")
        if raw is None:
            return None

        try:
            content = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            return CacheHit(
                served_from="L1_exact",
                content=content,
                degraded=False,
                staleness_seconds=0.0,
            )
        except (UnicodeDecodeError, AttributeError):
            return None

    async def store(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any] | None = None,
        content: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store a response in the L1 cache."""
        cache_key = self._build_key(model, messages, params)
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        await self._redis.set(
            f"{self._prefix}{cache_key}",
            content,
            ex=ttl,
        )

    def _build_key(
        self,
        model: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any] | None,
    ) -> str:
        """Build a deterministic SHA-256 cache key."""
        canonical = json.dumps(
            {"model": model, "messages": messages, "params": params or {}},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
