# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tiered semantic cache — L1 exact-match and L2 semantic similarity via Redis (Design §13.4).

L1: SHA-256 of canonical (model + prompt + params) as exact-match key.
L2: nearest cached prompt by cosine similarity, admitted only at or above the configured
    threshold (`SEMANTIC_CACHE_THRESHOLD`, default 0.95).

WHY L2 IS OPT-IN
----------------
L2 needs an embedding function, and embedding is a network call in production. A cache that
silently embedded on every miss would turn a cache miss into a provider request, which is the
opposite of the point. So `embed` is injected: pass it and L2 is live, omit it and this class
behaves exactly as the L1-only version did — same keys, same `AsyncRedisLike` surface, same
`served_from` value.

Criterion 14 requires "a near-duplicate prompt is served from L2 above the 0.95 threshold". Until
this commit that clause had no implementation: the module docstring read "Future phases add L2
semantic similarity matching" and `tests/integration/test_semantic_cache.py::test_l2_near_duplicate`
said in its own docstring that it "acts as a placeholder for the L2 implementation" and that the
near-duplicate "will miss until L2 is implemented". `core/config.py` carried
`semantic_cache_threshold = 0.95` and nothing read it.

D-44 SAYS ONLY REDACTED PROMPTS, AND THAT APPLIES TO L2 TOO
-----------------------------------------------------------
The embedding is computed from the same `RedactedPrompt` the key is computed from, never from raw
text, so no unredacted value reaches the embedding provider and none is stored in the index.
Q-13 asserts the key half of that; the same argument covers the vector because it is derived from
the identical input.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from src.secrets.redaction import RedactedPrompt


@dataclass(frozen=True)
class CacheHit:
    """Result returned when a cache lookup succeeds."""

    served_from: str  # e.g. "L1_exact", "L2_semantic"
    content: str
    degraded: bool = False
    staleness_seconds: float = 0.0
    similarity: float | None = None


@runtime_checkable
class AsyncRedisLike(Protocol):
    """Minimal async Redis interface for testability."""

    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: str | bytes, *, ex: int | None = None) -> Any: ...


@runtime_checkable
class AsyncRedisWithHashes(AsyncRedisLike, Protocol):
    """The extra surface L2 needs: a per-model index of prompt vectors.

    Separate from `AsyncRedisLike` on purpose. The L1-only path must keep working against a client
    that implements two methods, which is what `test_q13_cache_key.py`'s double provides; requiring
    hashes unconditionally would have broken it and forced the property test to grow a mock it does
    not need.
    """

    async def hset(self, name: str, key: str, value: str | bytes) -> Any: ...
    async def hgetall(self, name: str) -> dict[Any, Any]: ...


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, defined as 0.0 for a zero-magnitude vector rather than raising.

    A zero vector has no direction, so it is not similar to anything; returning 0.0 keeps it below
    every admissible threshold instead of producing a NaN that would compare false against the
    threshold for reasons a reader has to work out.
    """
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class TieredSemanticCache:
    """L1 exact-match plus optional L2 semantic-similarity cache backed by Redis.

    Key = SHA-256( canonical JSON of model + messages + params ).
    Value = JSON-encoded completion response content.
    """

    def __init__(
        self,
        *,
        redis: AsyncRedisLike,
        default_ttl_seconds: int = 3600,
        key_prefix: str = "ai:cache:l1:",
        embed: Callable[[str], Awaitable[Sequence[float]]] | None = None,
        similarity_threshold: float = 0.95,
        index_prefix: str = "ai:cache:l2:",
    ) -> None:
        self._redis = redis
        self._default_ttl = default_ttl_seconds
        self._prefix = key_prefix
        self._embed = embed
        self._threshold = similarity_threshold
        self._index_prefix = index_prefix

        if embed is not None:
            missing = [name for name in ("hset", "hgetall") if not hasattr(redis, name)]
            if missing:
                # Loud at construction, not on the first miss. A cache that accepted an embedder
                # and then silently never used it would report every near-duplicate as a miss and
                # look like a threshold problem.
                raise TypeError(
                    "L2 was requested (embed= was supplied) but the Redis client is missing "
                    f"{', '.join(missing)}; L2 needs a per-model vector index"
                )

    async def lookup(
        self,
        *,
        model: str,
        prompt: RedactedPrompt,
        params: dict[str, Any] | None = None,
    ) -> CacheHit | None:
        """Look up a cached response: exact match first, then semantic if L2 is enabled."""
        cache_key = self._build_key(model, prompt, params)
        raw = await self._redis.get(f"{self._prefix}{cache_key}")
        if raw is not None:
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

        if self._embed is None:
            return None
        return await self._lookup_semantic(model=model, prompt=prompt)

    async def _lookup_semantic(self, *, model: str, prompt: RedactedPrompt) -> CacheHit | None:
        """The L2 path: nearest stored prompt vector, admitted only at or above the threshold."""
        assert self._embed is not None  # guarded by the caller
        query_vector = list(await self._embed(str(prompt)))
        if not query_vector:
            return None

        index_name = self._index_name(model)
        stored = await self._redis.hgetall(index_name)  # type: ignore[attr-defined]
        if not stored:
            return None

        best_key: str | None = None
        best_score = -1.0
        for raw_key, raw_vector in stored.items():
            key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
            payload = raw_vector.decode("utf-8") if isinstance(raw_vector, bytes) else raw_vector
            try:
                candidate = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(candidate, list):
                continue
            score = cosine_similarity(query_vector, candidate)
            if score > best_score:
                best_score, best_key = score, key

        # `<` not `<=`: the threshold is inclusive, so an exact 0.95 is admitted. Criterion 14 says
        # "above the 0.95 threshold" and a strict comparison would make the configured value itself
        # unreachable, which is the sort of off-by-one that only shows up as a mysterious miss rate.
        if best_key is None or best_score < self._threshold:
            return None

        raw = await self._redis.get(f"{self._prefix}{best_key}")
        if raw is None:
            # The vector outlived its entry, which TTL expiry makes normal. Report a miss rather
            # than a hit with no content.
            return None
        content = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return CacheHit(
            served_from="L2_semantic",
            content=content,
            degraded=False,
            staleness_seconds=0.0,
            similarity=best_score,
        )

    async def store(
        self,
        *,
        model: str,
        prompt: RedactedPrompt,
        params: dict[str, Any] | None = None,
        content: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store a response in the L1 cache, and its vector in the L2 index when enabled."""
        cache_key = self._build_key(model, prompt, params)
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        await self._redis.set(
            f"{self._prefix}{cache_key}",
            content,
            ex=ttl,
        )

        if self._embed is None:
            return
        # Computed from the RedactedPrompt, never from raw text (D-44), so no unredacted value
        # reaches the embedding provider or the index.
        vector = list(await self._embed(str(prompt)))
        if not vector:
            return
        await self._redis.hset(  # type: ignore[attr-defined]
            self._index_name(model), cache_key, json.dumps(vector)
        )

    def _index_name(self, model: str) -> str:
        """One index per model: vectors from different models are not comparable."""
        digest = hashlib.sha256(model.encode("utf-8")).hexdigest()
        return f"{self._index_prefix}{digest}"

    def _build_key(
        self,
        model: str,
        prompt: RedactedPrompt,
        params: dict[str, Any] | None,
    ) -> str:
        """Build a deterministic SHA-256 cache key."""
        canonical = json.dumps(
            {"model": model, "prompt": prompt, "params": params or {}},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
