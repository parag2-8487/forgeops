# SPDX-License-Identifier: FSL-1.1-ALv2
"""Reranking: the real model path, and degradation that says it degraded (Leaf 13.2).

WHAT THIS FILE USED TO ASSERT:

    res = await reranker.rerank("query", [("doc1", 0.5)])
    assert res == [("doc1", 0.6)]

0.6 is 0.5 × 1.2. So the only test of this component pinned the fabrication exactly — and the fabrication
was a monotonic transform of the input score, which cannot reorder anything. A component whose entire
purpose is to change the order was tested by asserting the number it multiplied by.

`tasks.md` 13.2 was ticked with the words "Implement `VoyageReranker` calling `voyage-rerank-2` over the
shared `httpx` client with a BYO key". No such class existed.
"""

from __future__ import annotations

import httpx
import pytest
from src.core.reranker import (
    RERANK_MODEL,
    Reranker,
    RerankUnavailableError,
    VoyageReranker,
    rerank_or_degrade,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

CREDENTIAL = "pa-" + "test-not-a-real-key"


def transport(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


# ── the real model path ──────────────────────────────────────────────────────────────────────────


async def test_the_model_order_is_returned_and_can_differ_from_the_input() -> None:
    """The property the fabrication could not have: the output order is not the input order."""
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.update(json.loads(request.content.decode("utf-8")))
        # The LAST document sent is the most relevant, so a passthrough would fail this.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 2, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.42},
                    {"index": 1, "relevance_score": 0.11},
                ]
            },
        )

    reranker = VoyageReranker(credential=CREDENTIAL, base_url="https://api.voyage.test/v1")
    documents = {"a": "alpha text", "b": "beta text", "c": "gamma text"}
    async with transport(handler) as client:
        ranked = await reranker.rerank("which is gamma", documents, client=client)

    assert sent["model"] == RERANK_MODEL
    assert [d.doc_id for d in ranked] == ["c", "a", "b"]
    assert ranked[0].score == 0.91
    # The scores are the model's, not a transform of anything the caller supplied.
    assert [d.score for d in ranked] == [0.91, 0.42, 0.11]


async def test_top_k_is_forwarded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(request.content.decode("utf-8"))["top_k"] == 2
        return httpx.Response(200, json={"data": [{"index": 0, "relevance_score": 0.9}]})

    reranker = VoyageReranker(credential=CREDENTIAL)
    async with transport(handler) as client:
        await reranker.rerank("q", {"a": "x", "b": "y"}, top_k=2, client=client)


async def test_an_index_outside_the_documents_sent_is_refused() -> None:
    """Clamping would attribute one document's score to another."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 7, "relevance_score": 0.9}]})

    reranker = VoyageReranker(credential=CREDENTIAL)
    async with transport(handler) as client:
        with pytest.raises(RerankUnavailableError) as raised:
            await reranker.rerank("q", {"a": "x"}, client=client)
    assert "not sent" in str(raised.value)


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_a_non_200_raises_rather_than_inventing_an_order(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "must not be echoed"})

    reranker = VoyageReranker(credential=CREDENTIAL)
    async with transport(handler) as client:
        with pytest.raises(RerankUnavailableError) as raised:
            await reranker.rerank("q", {"a": "x"}, client=client)
    assert str(status) in str(raised.value)
    assert "must not be echoed" not in str(raised.value)


async def test_an_unconfigured_reranker_refuses() -> None:
    for credential in ("", "placeholder", "changeme"):
        reranker = VoyageReranker(credential=credential)
        assert reranker.configured is False
        with pytest.raises(RerankUnavailableError):
            await reranker.rerank("q", {"a": "x"})


# ── degradation that says so ─────────────────────────────────────────────────────────────────────


async def test_no_reranker_degrades_to_the_fused_order_and_reports_it() -> None:
    outcome = await rerank_or_degrade(None, "q", {"a": "x", "b": "y"}, fused_order=("b", "a"))
    assert outcome.degraded is True
    assert outcome.reason
    assert [d.doc_id for d in outcome.documents] == ["b", "a"]
    # NO INVENTED SCORE. The model did not run, so there is no relevance score to report.
    assert all(d.score == 0.0 for d in outcome.documents)


async def test_a_failing_reranker_degrades_and_names_the_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    reranker = VoyageReranker(credential=CREDENTIAL)
    async with transport(handler) as client:
        outcome = await rerank_or_degrade(reranker, "q", {"a": "x", "b": "y"}, fused_order=("a", "b"), client=client)
    assert outcome.degraded is True
    assert "503" in outcome.reason
    assert [d.doc_id for d in outcome.documents] == ["a", "b"]


async def test_a_successful_rerank_is_not_marked_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 1, "relevance_score": 0.8}]})

    reranker = VoyageReranker(credential=CREDENTIAL)
    async with transport(handler) as client:
        outcome = await rerank_or_degrade(reranker, "q", {"a": "x", "b": "y"}, fused_order=("a", "b"), client=client)
    assert outcome.degraded is False
    assert [d.doc_id for d in outcome.documents] == ["b"]


async def test_degradation_respects_top_k() -> None:
    outcome = await rerank_or_degrade(None, "q", {"a": "x", "b": "y", "c": "z"}, fused_order=("c", "b", "a"), top_k=2)
    assert [d.doc_id for d in outcome.documents] == ["c", "b"]


async def test_an_id_absent_from_the_documents_is_dropped_from_the_order() -> None:
    """A fused order may name a document that was filtered out before reranking."""
    outcome = await rerank_or_degrade(None, "q", {"a": "x"}, fused_order=("ghost", "a"))
    assert [d.doc_id for d in outcome.documents] == ["a"]


# ── the Q-29 seam ────────────────────────────────────────────────────────────────────────────────


async def test_the_pair_interface_returns_the_input_scores_when_degraded() -> None:
    """Replaces `assert res == [("doc1", 0.6)]`. 0.6 was 0.5 x 1.2 — the fabrication itself."""
    reranker = Reranker()
    result = await reranker.rerank("query", [("doc1", 0.5), ("doc2", 0.8)])
    assert result == [("doc1", 0.5), ("doc2", 0.8)], "a degraded rerank must not alter the scores it was given"


async def test_the_pair_interface_never_grows_or_invents() -> None:
    """The two properties Q-29 asserts, stated here directly as well."""
    reranker = Reranker()
    candidates = [("doc1", 0.5), ("doc2", 0.8), ("doc3", 0.1)]
    result = await reranker.rerank("query", candidates)
    assert len(result) <= len(candidates)
    assert {doc_id for doc_id, _ in result} <= {doc_id for doc_id, _ in candidates}


async def test_the_threshold_still_filters() -> None:
    reranker = Reranker(score_threshold=0.4)
    result = await reranker.rerank("query", [("low", 0.1), ("high", 0.9)])
    assert [doc_id for doc_id, _ in result] == ["high"]


async def test_an_empty_candidate_list_is_empty() -> None:
    assert await Reranker().rerank("query", []) == []


async def test_fallback_disabled_raises_rather_than_passing_through() -> None:
    """`enable_fallback=False` must be a refusal, not a silent passthrough."""
    reranker = Reranker(enable_fallback=False)
    with pytest.raises(RerankUnavailableError):
        await reranker.rerank("query", [("doc1", 0.5)])
