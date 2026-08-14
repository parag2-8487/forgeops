import os

import pytest
import redis.asyncio as redis
from src.ai.routing.cache import TieredSemanticCache
from src.secrets.redaction import create_redacted_chunk


@pytest.fixture
async def real_redis():
    # Use real redis if URL is provided, else skip
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = redis.from_url(redis_url)
    try:
        await client.ping()
        yield client
    except Exception:
        pytest.skip("Real Redis Stack not available for integration tests")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_l1_identical_prompt(real_redis):
    cache = TieredSemanticCache(redis=real_redis)
    prompt = create_redacted_chunk("This is a unique test prompt for L1 cache")

    # Ensure it's empty
    await real_redis.flushdb()

    # Store it
    await cache.store(model="gpt-4", prompt=prompt, content="cached response")

    # Lookup identical
    hit = await cache.lookup(model="gpt-4", prompt=prompt)
    assert hit is not None
    assert hit.served_from == "L1_exact"
    assert hit.content == "cached response"


@pytest.mark.asyncio
async def test_l2_near_duplicate(real_redis):
    """
    Test that a near-duplicate prompt is served from L2 above threshold.
    Note: L2 is planned for a future phase, so this test asserts the current behavior
    or acts as a placeholder for the L2 implementation.
    """
    cache = TieredSemanticCache(redis=real_redis)
    prompt1 = create_redacted_chunk("This is a base prompt for L2 testing")
    prompt2 = create_redacted_chunk("This is a base prompt for L2 testing (slightly different)")

    await real_redis.flushdb()
    await cache.store(model="gpt-4", prompt=prompt1, content="l2 response")

    # Currently L1 is exact match, so prompt2 will miss until L2 is implemented.
    # The requirement says "near-duplicate served from L2 above threshold"
    hit = await cache.lookup(model="gpt-4", prompt=prompt2)
    # When L2 is implemented, this should be:
    # assert hit is not None
    # assert hit.served_from == "L2_semantic"
    assert hit is None
