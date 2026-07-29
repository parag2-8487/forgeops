# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test P-06: TtlToolCache — TTL clamping, no-cache, and never-serve-after-expiry.

Compares TtlToolCache behavior against a pure reference model to validate
correctness of clamping and expiry semantics.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.mcp.cache import TtlToolCache

# --- Pure reference model ---


class ReferenceCacheModel:
    """Pure reference model for TtlToolCache behavior validation.

    This model defines the correct behavior without Redis — we use it as
    an oracle to compare against the real implementation.
    """

    def __init__(self, max_ttl_ms: int = 60_000):
        self.max_ttl_ms = max_ttl_ms

    def should_store(self, server_ttl_ms: int) -> bool:
        """Whether a put() call should actually store anything."""
        effective_ttl = min(server_ttl_ms, self.max_ttl_ms)
        return effective_ttl > 0

    def effective_ttl(self, server_ttl_ms: int) -> int:
        """The TTL that should be applied (or 0 if no storage)."""
        if not self.should_store(server_ttl_ms):
            return 0
        return min(server_ttl_ms, self.max_ttl_ms)


# --- Fake Redis for property testing ---


class FakeRedis:
    """In-memory Redis mock with TTL tracking for property tests."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._ttls: dict[str, int] = {}  # milliseconds remaining
        self._expired: set[str] = set()

    async def set(self, name: str, value: str, px: int | None = None):
        self._store[name] = value
        if px is not None:
            self._ttls[name] = px
        self._expired.discard(name)

    async def get(self, name: str) -> str | None:
        if name in self._expired:
            return None
        return self._store.get(name)

    async def pttl(self, name: str) -> int:
        if name in self._expired:
            return -2  # Key does not exist
        if name not in self._ttls:
            return -1  # No TTL
        return self._ttls.get(name, -2)

    def simulate_expiry(self, name: str):
        """Simulate that a key has expired."""
        self._expired.add(name)
        self._ttls.pop(name, None)

    def has_key(self, name: str) -> bool:
        return name in self._store and name not in self._expired

    def get_stored_ttl(self, name: str) -> int | None:
        if name in self._expired:
            return None
        return self._ttls.get(name)


# --- Strategies ---

ttl_strategy = st.integers(min_value=-10_000, max_value=200_000)
max_ttl_strategy = st.integers(min_value=1, max_value=120_000)
key_strategy = st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop_")
value_strategy = st.text(min_size=1, max_size=100)


# --- P-06a: Non-positive TTL creates no key ---


@given(
    server_ttl=st.integers(min_value=-100_000, max_value=0),
    max_ttl=max_ttl_strategy,
    key=key_strategy,
    value=value_strategy,
)
@settings(max_examples=200)
@pytest.mark.asyncio
async def test_nonpositive_ttl_no_key(server_ttl, max_ttl, key, value):
    """Non-positive effective TTL (server_ttl <= 0) creates no key."""
    redis = FakeRedis()
    cache = TtlToolCache(redis, max_ttl_ms=max_ttl)
    ref = ReferenceCacheModel(max_ttl_ms=max_ttl)

    tools = [{"name": value}]
    stored = await cache.put(key, tools, server_ttl)

    # Reference says: no storage
    assert not ref.should_store(server_ttl)
    # Actual behavior matches
    assert stored is False
    assert not redis.has_key(cache.key_for(key))


# --- P-06b: min(server_ttl, max_ttl) clamping ---


@given(
    server_ttl=st.integers(min_value=1, max_value=200_000),
    max_ttl=max_ttl_strategy,
    key=key_strategy,
    value=value_strategy,
)
@settings(max_examples=200)
@pytest.mark.asyncio
async def test_ttl_clamping(server_ttl, max_ttl, key, value):
    """Effective TTL is always min(server_ttl, max_ttl) for positive TTLs."""
    redis = FakeRedis()
    cache = TtlToolCache(redis, max_ttl_ms=max_ttl)
    ref = ReferenceCacheModel(max_ttl_ms=max_ttl)

    tools = [{"name": value}]
    stored = await cache.put(key, tools, server_ttl)

    expected_ttl = ref.effective_ttl(server_ttl)
    assert ref.should_store(server_ttl)
    assert stored is True
    assert redis.get_stored_ttl(cache.key_for(key)) == expected_ttl


# --- P-06c: Reference model agreement ---


@given(
    server_ttl=ttl_strategy,
    max_ttl=max_ttl_strategy,
    key=key_strategy,
    value=value_strategy,
)
@settings(max_examples=200)
@pytest.mark.asyncio
async def test_reference_model_agreement(server_ttl, max_ttl, key, value):
    """Cache behavior matches the pure reference model for all TTL values."""
    redis = FakeRedis()
    cache = TtlToolCache(redis, max_ttl_ms=max_ttl)
    ref = ReferenceCacheModel(max_ttl_ms=max_ttl)

    tools = [{"name": value}]
    stored = await cache.put(key, tools, server_ttl)

    if ref.should_store(server_ttl):
        assert stored is True
        assert redis.has_key(cache.key_for(key))
        assert redis.get_stored_ttl(cache.key_for(key)) == ref.effective_ttl(server_ttl)
    else:
        assert stored is False
        assert not redis.has_key(cache.key_for(key))


# --- P-06d: Never serve after expiry ---


@given(
    server_ttl=st.integers(min_value=1, max_value=100_000),
    max_ttl=max_ttl_strategy,
    key=key_strategy,
    value=value_strategy,
)
@settings(max_examples=200)
@pytest.mark.asyncio
async def test_never_serve_after_expiry(server_ttl, max_ttl, key, value):
    """After TTL expiry, cache NEVER returns the value (Redis PTTL <= 0 → None)."""
    redis = FakeRedis()
    cache = TtlToolCache(redis, max_ttl_ms=max_ttl)

    tools = [{"name": value}]
    # Store a value
    await cache.put(key, tools, server_ttl)

    # Before expiry: should return value
    result_before = await cache.get(key)
    assert result_before == tools

    # Simulate expiry
    redis.simulate_expiry(cache.key_for(key))

    # After expiry: must return None (never serve stale)
    result_after = await cache.get(key)
    assert result_after is None


# --- P-06e: Large max_ttl still clamps to server_ttl ---


@given(
    server_ttl=st.integers(min_value=1, max_value=50_000),
    key=key_strategy,
    value=value_strategy,
)
@settings(max_examples=100)
@pytest.mark.asyncio
async def test_server_ttl_respected_when_smaller(server_ttl, key, value):
    """When server_ttl < max_ttl, the effective TTL is server_ttl."""
    max_ttl = 999_999  # Very large
    redis = FakeRedis()
    cache = TtlToolCache(redis, max_ttl_ms=max_ttl)

    tools = [{"name": value}]
    await cache.put(key, tools, server_ttl)
    assert redis.get_stored_ttl(cache.key_for(key)) == server_ttl


# --- P-06f: max_ttl < server_ttl clamps down ---


@given(
    max_ttl=st.integers(min_value=1, max_value=10_000),
    key=key_strategy,
    value=value_strategy,
)
@settings(max_examples=100)
@pytest.mark.asyncio
async def test_max_ttl_clamps_down(max_ttl, key, value):
    """When max_ttl < server_ttl, the effective TTL is max_ttl."""
    server_ttl = 999_999  # Very large
    redis = FakeRedis()
    cache = TtlToolCache(redis, max_ttl_ms=max_ttl)

    tools = [{"name": value}]
    await cache.put(key, tools, server_ttl)
    assert redis.get_stored_ttl(cache.key_for(key)) == max_ttl
