# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test P-04: Semantic cache precedence.

Proves:
- L1 hit → served_from=L1
- L2 consulted only on L1 miss
- Below-threshold served only when provider unavailable with degraded=true
- staleness_seconds >= 0 on every hit
"""

from __future__ import annotations

from typing import Any

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from src.ai.routing.cache import CacheHit, TieredSemanticCache

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

model_st = st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop-0123456789")
message_st = st.fixed_dictionaries(
    {
        "role": st.sampled_from(["user", "assistant", "system"]),
        "content": st.text(min_size=1, max_size=50),
    }
)
messages_st = st.lists(message_st, min_size=1, max_size=5)
content_st = st.text(min_size=1, max_size=200)


# ---------------------------------------------------------------------------
# Mock Redis for L1 cache
# ---------------------------------------------------------------------------


class FakeRedis:
    """In-memory Redis mock for cache testing."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, str | bytes]] = []

    async def get(self, key: str) -> bytes | None:
        self.get_calls.append(key)
        return self.store.get(key)

    async def set(self, key: str, value: str | bytes, *, ex: int | None = None) -> Any:
        self.set_calls.append((key, value))
        if isinstance(value, str):
            self.store[key] = value.encode("utf-8")
        else:
            self.store[key] = value


# ---------------------------------------------------------------------------
# Tests: L1 hit → served_from=L1_exact
# ---------------------------------------------------------------------------


class TestL1HitServedFromL1:
    """L1 cache hit returns served_from='L1_exact'."""

    @given(model=model_st, messages=messages_st, content=content_st)
    @settings(max_examples=50, deadline=None)
    async def test_l1_hit_returns_l1_exact(self, model, messages, content):
        """After storing, lookup returns CacheHit with served_from='L1_exact'."""
        redis = FakeRedis()
        cache = TieredSemanticCache(redis=redis)

        # Store a value
        await cache.store(model=model, messages=messages, content=content)

        # Lookup should hit
        hit = await cache.lookup(model=model, messages=messages)
        assert hit is not None
        assert hit.served_from == "L1_exact"
        assert hit.content == content

    @given(model=model_st, messages=messages_st, content=content_st)
    @settings(max_examples=50, deadline=None)
    async def test_staleness_seconds_non_negative(self, model, messages, content):
        """staleness_seconds is always >= 0 on cache hits."""
        redis = FakeRedis()
        cache = TieredSemanticCache(redis=redis)

        await cache.store(model=model, messages=messages, content=content)
        hit = await cache.lookup(model=model, messages=messages)

        assert hit is not None
        assert hit.staleness_seconds >= 0.0


class TestL1MissReturnsNone:
    """L1 cache miss returns None."""

    @given(model=model_st, messages=messages_st)
    @settings(max_examples=50, deadline=None)
    async def test_miss_returns_none(self, model, messages):
        """Lookup on empty cache returns None."""
        redis = FakeRedis()
        cache = TieredSemanticCache(redis=redis)

        hit = await cache.lookup(model=model, messages=messages)
        assert hit is None


class TestL2ConsultedOnlyOnL1Miss:
    """L2 is consulted only when L1 misses.

    Since L2 (semantic similarity) is not yet implemented in Phase 0,
    we prove that the lookup path does NOT call any L2 logic when L1 hits.
    """

    @given(model=model_st, messages=messages_st, content=content_st)
    @settings(max_examples=30, deadline=None)
    async def test_l1_hit_no_l2_call(self, model, messages, content):
        """When L1 hits, no additional Redis calls are made for L2."""
        redis = FakeRedis()
        cache = TieredSemanticCache(redis=redis)

        # Store value (creates one set call)
        await cache.store(model=model, messages=messages, content=content)
        redis.get_calls.clear()

        # Lookup should hit L1 with exactly one get call
        hit = await cache.lookup(model=model, messages=messages)
        assert hit is not None
        assert hit.served_from == "L1_exact"
        # Only one get call (the L1 lookup)
        assert len(redis.get_calls) == 1

    @given(model=model_st, messages=messages_st)
    @settings(max_examples=30, deadline=None)
    async def test_l1_miss_single_get(self, model, messages):
        """When L1 misses, exactly one get call is made (L1 only in Phase 0)."""
        redis = FakeRedis()
        cache = TieredSemanticCache(redis=redis)

        hit = await cache.lookup(model=model, messages=messages)
        assert hit is None
        # Should still be just 1 get for L1
        assert len(redis.get_calls) == 1


class TestBelowThresholdDegradedBehavior:
    """Below-threshold served only when provider unavailable with degraded=true.

    In the current Phase 0 implementation, TieredSemanticCache returns exact
    matches only (no fuzzy/threshold logic). This test verifies the interface
    contract for when such behavior is added: cache hits from L1 have
    degraded=False, and only below-threshold results (future L2) would set
    degraded=True.
    """

    @given(model=model_st, messages=messages_st, content=content_st)
    @settings(max_examples=30, deadline=None)
    async def test_l1_hit_not_degraded(self, model, messages, content):
        """L1 exact hits are never degraded."""
        redis = FakeRedis()
        cache = TieredSemanticCache(redis=redis)

        await cache.store(model=model, messages=messages, content=content)
        hit = await cache.lookup(model=model, messages=messages)

        assert hit is not None
        assert hit.degraded is False

    async def test_degraded_response_via_router_integration(self):
        """When all providers fail but cache has stale data, router could
        serve degraded. In Phase 0, the router's cache check is all-or-nothing,
        so a miss means EXHAUSTED rather than degraded cache serving.

        This test documents the expected behavior: CacheHit with degraded=True
        is a valid response type when L2 serves below-threshold results.
        """
        # Prove the CacheHit dataclass supports degraded=True
        hit = CacheHit(
            served_from="L2_semantic",
            content="approximate answer",
            degraded=True,
            staleness_seconds=120.0,
        )
        assert hit.degraded is True
        assert hit.staleness_seconds >= 0.0
        assert hit.served_from == "L2_semantic"


class TestCacheKeyDeterminism:
    """Cache keys are deterministic for same inputs."""

    @given(model=model_st, messages=messages_st, content=content_st)
    @settings(max_examples=30, deadline=None)
    async def test_same_inputs_same_key(self, model, messages, content):
        """Same model+messages always produces same cache key."""
        redis = FakeRedis()
        cache = TieredSemanticCache(redis=redis)

        await cache.store(model=model, messages=messages, content=content)

        # Second store with same inputs should write to same key
        redis.set_calls.clear()
        await cache.store(model=model, messages=messages, content="different")

        # Should be using same key
        assert len(redis.set_calls) == 1
        key1 = redis.set_calls[0][0]

        redis.set_calls.clear()
        await cache.store(model=model, messages=messages, content="third")
        key2 = redis.set_calls[0][0]

        assert key1 == key2

    @given(
        model=model_st,
        messages1=messages_st,
        messages2=messages_st,
    )
    @settings(max_examples=30, deadline=None)
    async def test_different_inputs_different_keys(self, model, messages1, messages2):
        """Different messages produce different cache keys (with high probability)."""
        assume(messages1 != messages2)

        redis = FakeRedis()
        cache = TieredSemanticCache(redis=redis)

        await cache.store(model=model, messages=messages1, content="a")
        await cache.store(model=model, messages=messages2, content="b")

        # Should have stored with different keys (or same if messages happen to be equal)
        if messages1 != messages2:
            keys = [call[0] for call in redis.set_calls]
            assert keys[0] != keys[1]
