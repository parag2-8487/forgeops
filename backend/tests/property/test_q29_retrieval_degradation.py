# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test Q-29: Retrieval degradation safety (Leaf 13.13)."""

import pytest
from hypothesis import given, strategies as st
from src.ai.reranker import Reranker

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

@given(candidates=st.lists(st.tuples(st.text(min_size=1, max_size=10), st.floats(min_value=0.0, max_value=1.0)), min_size=0, max_size=10))
async def test_property_q29_retrieval_degradation(candidates: list[tuple[str, float]]):
    reranker = Reranker(enable_fallback=True)
    res = await reranker.rerank("query", candidates)
    assert isinstance(res, list)
    assert len(res) <= len(candidates)
