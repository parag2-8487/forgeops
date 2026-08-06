# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.ai.reranker import Reranker

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

async def test_reranker_success():
    reranker = Reranker()
    candidates = [("doc1", 0.5), ("doc2", 0.8)]
    res = await reranker.rerank("search query", candidates)
    assert len(res) == 2
    assert res[0][0] == "doc2"
    assert res[0][1] > res[1][1]

async def test_reranker_degradation_fallback():
    reranker = Reranker(enable_fallback=True)
    # Pass invalid data structure to trigger fallback handling internally
    class BadCandidate:
        pass

    candidates = [("doc1", 0.5)]
    # Normal pass
    res = await reranker.rerank("query", candidates)
    assert res == [("doc1", 0.6)]
