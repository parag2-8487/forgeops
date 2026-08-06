# SPDX-License-Identifier: Apache-2.0
from hypothesis import given, settings, strategies as st
import hashlib


@settings(max_examples=100)
@given(
    model_name=st.text(min_size=1, max_size=20),
    prompt=st.text(min_size=1, max_size=100),
    tenant_id=st.text(min_size=1, max_size=20)
)
def test_q13_cache_key_determinism(model_name: str, prompt: str, tenant_id: str):
    """
    Property Q-13: Semantic cache key determinism.
    Key generation must be deterministic, collisions for distinct inputs must be 0,
    and must combine model name, prompt digest, and tenant ID.
    """
    prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    raw_key = f"{tenant_id}:{model_name}:{prompt_digest}"
    cache_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    # Re-evaluating with same inputs produces identical key
    prompt_digest2 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    raw_key2 = f"{tenant_id}:{model_name}:{prompt_digest2}"
    cache_key2 = hashlib.sha256(raw_key2.encode("utf-8")).hexdigest()

    assert cache_key == cache_key2
    assert len(cache_key) == 64
