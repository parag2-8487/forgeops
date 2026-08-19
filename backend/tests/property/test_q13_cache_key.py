# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test Q-13: the semantic cache is keyed over redacted prompts (leaf 13.10).

Appendix B states the property as:

    ∀ prompts: every cache key is computed over a `RedactedPrompt`; no cached completion is
    retrievable using unredacted text; no cache entry's stored key material contains a
    synthetic secret.

WHAT THIS FILE REPLACES, AND WHY IT HAD TO BE REPLACED
------------------------------------------------------
The previous version of this file imported `hashlib` and `hypothesis` and nothing else. It
computed a SHA-256 twice over the same string and asserted the two digests were equal:

    assert cache_key == cache_key2
    assert len(cache_key) == 64

That is `sha256(x) == sha256(x)` — true for every input, in every build, forever. It exercised
no production code at all, so `TieredSemanticCache` could have been deleted outright and this
file would still have passed. It also meant Q-13 could not be given a negative control:
`scripts/mutation-harness.py` reports a property that survives its own mutation as `VACUOUS`,
and there is no mutation of the cache that a test which never calls the cache can detect.

This version drives `TieredSemanticCache` itself. The Redis double is a dict — the property is
about key COMPUTATION, and a real server would add latency without adding an assertion. The
real-Redis path is covered separately by
`tests/integration/test_semantic_cache.py::test_l1_identical_prompt`.

THE CLAUSE THAT CARRIES THE PROPERTY
------------------------------------
`test_a_cached_completion_is_not_retrievable_with_unredacted_text` is the one that matters. A
cache keyed over raw text would let the unredacted prompt — the one still containing the secret
— pull back a completion stored under its redacted form, which is precisely the leak the
`RedactedPrompt` type exists to prevent. Storing under the redacted prompt and then looking up
with the raw text must MISS.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.ai.routing.cache import TieredSemanticCache
from src.secrets.redaction import create_redacted_prompt

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


class _DictRedis:
    """The smallest thing satisfying `AsyncRedisLike`, so the keys are inspectable.

    Every key written is retained in `self.store`, which is what lets the third property below
    assert on the KEY MATERIAL rather than on the value.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> bytes | None:
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    async def set(self, key: str, value: str | bytes, *, ex: int | None = None) -> Any:
        self.store[key] = value.decode("utf-8") if isinstance(value, bytes) else value
        return True


#: A synthetic secret with NO provider shape, deliberately.
#:
#: The first version of this helper returned a classic-PAT-shaped value assembled with `+`.
#: `scripts/check-test-credentials.py` constant-folds adjacent string literals, so the assembly did
#: not hide anything and CI failed with "literal resembling a GitHub token" (FO-SEC001) — the gate
#: working exactly as intended.
#:
#: The shape bought nothing here. Q-13's assertions turn on a secret that was DECLARED to the
#: redactor through `project_secrets`, which is replaced by exact match whatever it looks like; the
#: provider-pattern half of redaction is Q-24's subject and is tested there. So the value carries the
#: repository's synthetic marker and resembles no vendor's credential.
def _synthetic_secret() -> str:
    return "test-only-not-a-real-secret-" + ("a1b2c3d4" * 3)


@settings(max_examples=100)
@given(
    model=st.text(min_size=1, max_size=20),
    surrounding=st.text(min_size=1, max_size=60),
)
async def test_a_cached_completion_is_not_retrievable_with_unredacted_text(
    model: str, surrounding: str
) -> None:
    """The property: the redacted prompt is the only key that reaches the entry."""
    secret = _synthetic_secret()
    raw_text = f"{surrounding} {secret} {surrounding}"
    redacted = create_redacted_prompt(raw_text, [secret])

    # The redaction must actually have happened, or the two "different" prompts below are the
    # same string and the assertion that follows is vacuous. This is the positive control.
    assert secret not in redacted, "redaction did not remove the synthetic secret"

    cache = TieredSemanticCache(redis=_DictRedis())
    await cache.store(model=model, prompt=redacted, content="a completion")

    # Stored under the redacted prompt, so the redacted prompt finds it...
    hit = await cache.lookup(model=model, prompt=redacted)
    assert hit is not None, "the redacted prompt must find its own entry"
    assert hit.served_from == "L1_exact"

    # ...and the UNREDACTED text must not.
    leaked = await cache.lookup(model=model, prompt=raw_text)  # type: ignore[arg-type]
    assert leaked is None, (
        "Q-13 violation: a completion stored under a RedactedPrompt was retrieved using the "
        "unredacted text, so the cache key is not computed over the redacted form"
    )


@settings(max_examples=100)
@given(
    model=st.text(min_size=1, max_size=20),
    prompt_a=st.text(min_size=1, max_size=60),
    prompt_b=st.text(min_size=1, max_size=60),
)
async def test_distinct_prompts_do_not_share_a_cache_entry(
    model: str, prompt_a: str, prompt_b: str
) -> None:
    """Two different prompts must not collide, or one caller reads another's completion."""
    redacted_a = create_redacted_prompt(prompt_a, [])
    redacted_b = create_redacted_prompt(prompt_b, [])

    cache = TieredSemanticCache(redis=_DictRedis())
    await cache.store(model=model, prompt=redacted_a, content="completion for A")

    hit_b = await cache.lookup(model=model, prompt=redacted_b)
    if prompt_a == prompt_b:
        # Hypothesis will draw equal pairs; then a hit is correct, not a collision.
        assert hit_b is not None
        return
    assert hit_b is None, (
        "Q-13 violation: prompt B read the entry stored for prompt A, so the key does not "
        "depend on the prompt"
    )


@settings(max_examples=100)
@given(
    model=st.text(min_size=1, max_size=20),
    surrounding=st.text(min_size=1, max_size=60),
)
async def test_no_stored_key_material_contains_the_secret(model: str, surrounding: str) -> None:
    """The key is a digest, so the secret must not survive into the key itself."""
    secret = _synthetic_secret()
    redacted = create_redacted_prompt(f"{surrounding} {secret}", [secret])

    redis = _DictRedis()
    cache = TieredSemanticCache(redis=redis)
    await cache.store(model=model, prompt=redacted, content="a completion")

    assert redis.store, "nothing was written, so this proves nothing"
    for key in redis.store:
        assert secret not in key, (
            "Q-13 violation: the synthetic secret appears in stored cache key material"
        )
        # And the key is a real digest rather than the prompt in disguise.
        digest = key.rsplit(":", 1)[-1]
        assert len(digest) == len(hashlib.sha256(b"").hexdigest())
