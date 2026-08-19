# SPDX-License-Identifier: FSL-1.1-ALv2
"""Criterion 14: Redis semantic caching, both tiers, against a real Redis Stack.

Appendix E's bar for criterion 14 is: "the same generation prompt twice and the second is served
from L1 with zero provider calls, then a near-duplicate prompt is served from L2 above the 0.95
threshold".

`test_l2_near_duplicate` used to say in its own docstring that it "acts as a placeholder for the L2
implementation" and that "prompt2 will miss until L2 is implemented", because L2 did not exist. It
does now, and this file asserts the bar rather than describing it.

WHY THE EMBEDDER IS INJECTED AND DETERMINISTIC
----------------------------------------------
Embedding is a network call in production. A test that reached a provider would be slow, flaky and
dependent on a key, and this repository's standing pattern for that is a local fixture rather than a
mock of the thing under test (`test_generation_service.py` and the Phase 0 pattern
`REVIEW-PHASE-0.md` singled out). The embedder here is a small deterministic bag-of-words vector, so
"near-duplicate" is a property of the text rather than of a model's mood, and the threshold
comparison — which is the part criterion 14 is about — is exercised exactly.

ZERO PROVIDER CALLS IS ASSERTED, NOT ASSUMED
--------------------------------------------
The embedder counts its own invocations. The L1 half asserts the count does not move, which is the
"zero provider calls" clause; without that the L1 test would pass for a cache that re-embedded on
every hit.
"""

from __future__ import annotations

import os

import pytest
import redis.asyncio as redis
from src.ai.routing.cache import TieredSemanticCache, cosine_similarity
from src.secrets.redaction import create_redacted_chunk, create_redacted_prompt


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


class _CountingEmbedder:
    """A deterministic bag-of-words embedder that records how often it was called.

    Deliberately crude: similarity here must be a function of shared words so that a
    "near-duplicate" is near by construction and the threshold is what decides admission. A learned
    model would make the assertion depend on its weights.
    """

    #: The vocabulary is fixed so vectors are comparable across calls.
    VOCAB = (
        "this",
        "is",
        "a",
        "base",
        "prompt",
        "for",
        "testing",
        "slightly",
        "different",
        "entirely",
        "unrelated",
        "subject",
        "matter",
    )

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, text: str) -> list[float]:
        self.calls += 1
        words = text.lower().replace("(", " ").replace(")", " ").split()
        counts = {token: 0.0 for token in self.VOCAB}
        for word in words:
            stripped = word.strip(".,!?;:")
            if stripped in counts:
                counts[stripped] += 1.0
        return [counts[token] for token in self.VOCAB]


@pytest.mark.asyncio
async def test_l1_identical_prompt(real_redis):
    cache = TieredSemanticCache(redis=real_redis)
    prompt = create_redacted_chunk("This is a unique test prompt for L1 cache")

    await cache.store(model="gpt-4", prompt=prompt, content="cached response")
    hit = await cache.lookup(model="gpt-4", prompt=prompt)

    assert hit is not None
    assert hit.served_from == "L1_exact"
    assert hit.content == "cached response"


@pytest.mark.asyncio
async def test_l1_repeat_costs_zero_provider_calls(real_redis):
    """The 'zero provider calls' half of the criterion, asserted on the embedder's counter."""
    embedder = _CountingEmbedder()
    cache = TieredSemanticCache(redis=real_redis, embed=embedder, similarity_threshold=0.95)
    prompt = create_redacted_prompt("This is a base prompt for testing", [])

    await cache.store(model="gpt-4-zero", prompt=prompt, content="first response")
    calls_after_store = embedder.calls

    hit = await cache.lookup(model="gpt-4-zero", prompt=prompt)
    assert hit is not None
    assert hit.served_from == "L1_exact"
    # An exact hit must short-circuit before L2, so nothing is embedded on the read path.
    assert embedder.calls == calls_after_store, (
        "an L1 exact hit embedded the prompt; a cache hit must not cost a provider call"
    )


@pytest.mark.asyncio
async def test_l2_near_duplicate(real_redis):
    """A near-duplicate is served from L2 at or above the threshold. Criterion 14's second clause.

    The near-duplicate differs in SURFACE FORM — casing and punctuation — while carrying the same
    content. That is the case a semantic cache exists to catch and an exact cache cannot: the two
    strings hash differently, so L1 must miss, and only similarity can serve it.

    It is also the honest choice for this embedder. A bag-of-words count vector penalises added
    words hard: appending "(slightly different)" to a seven-word prompt scores 0.88, below the 0.95
    the criterion mandates. A learned model would rate that pair far closer, but the fixture is not
    a learned model, and the alternative — lowering the threshold until the pair passed — would be
    tuning the criterion to fit the test. The threshold stays at 0.95 and the pair is one this
    embedder genuinely rates as near.
    """
    embedder = _CountingEmbedder()
    cache = TieredSemanticCache(
        redis=real_redis,
        embed=embedder,
        similarity_threshold=0.95,
        key_prefix="ai:cache:l1:q14test:",
        index_prefix="ai:cache:l2:q14test:",
    )

    prompt1 = create_redacted_prompt("This is a base prompt for testing", [])
    prompt2 = create_redacted_prompt("this is a Base Prompt for TESTING!!", [])

    await cache.store(model="gpt-4-l2", prompt=prompt1, content="l2 response")

    # The positive control: the two prompts must NOT be exact-equal, or an L1 hit would be
    # mistaken for an L2 hit and the clause would be untested.
    assert str(prompt1) != str(prompt2)

    hit = await cache.lookup(model="gpt-4-l2", prompt=prompt2)

    assert hit is not None, "the near-duplicate was not served from L2"
    assert hit.served_from == "L2_semantic"
    assert hit.content == "l2 response"
    assert hit.similarity is not None and hit.similarity >= 0.95


@pytest.mark.asyncio
async def test_l2_refuses_a_dissimilar_prompt(real_redis):
    """The threshold has to exclude something, or L2 is a cache that returns anything."""
    embedder = _CountingEmbedder()
    cache = TieredSemanticCache(
        redis=real_redis,
        embed=embedder,
        similarity_threshold=0.95,
        key_prefix="ai:cache:l1:q14miss:",
        index_prefix="ai:cache:l2:q14miss:",
    )

    stored = create_redacted_prompt("This is a base prompt for testing", [])
    unrelated = create_redacted_prompt("entirely unrelated subject matter", [])

    await cache.store(model="gpt-4-miss", prompt=stored, content="should not be served")

    hit = await cache.lookup(model="gpt-4-miss", prompt=unrelated)
    assert hit is None, "L2 served a dissimilar prompt, so the threshold admits anything"


def test_cosine_similarity_is_zero_for_a_zero_vector():
    """A zero vector has no direction, so it must not be similar to anything."""
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
