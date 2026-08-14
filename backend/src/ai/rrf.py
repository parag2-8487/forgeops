# SPDX-License-Identifier: FSL-1.1-ALv2
"""Reciprocal Rank Fusion (RRF) for hybrid search (Leaf 13.1)."""

from __future__ import annotations


def reciprocal_rank_fusion(
    bm25_ranks: list[str],
    vector_ranks: list[str],
    k: int = 60,
    top_n: int = 10,
) -> list[tuple[str, float]]:
    """Merge sparse (BM25) and dense (vector) rank lists using RRF.

    Score(d) = sum(1 / (k + rank(d)))
    """
    scores: dict[str, float] = {}

    for rank, doc_id in enumerate(bm25_ranks, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + (1.0 / (k + rank))

    for rank, doc_id in enumerate(vector_ranks, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + (1.0 / (k + rank))

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked[:top_n]
