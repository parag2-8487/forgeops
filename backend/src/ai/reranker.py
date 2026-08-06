# SPDX-License-Identifier: FSL-1.1-ALv2
"""Reranker with explicit graceful degradation to RRF order (Leaf 13.2)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class Reranker:
    def __init__(self, score_threshold: float = 0.0, enable_fallback: bool = True) -> None:
        self.score_threshold = score_threshold
        self.enable_fallback = enable_fallback

    async def rerank(
        self, query: str, candidates: list[tuple[str, float]]
    ) -> list[tuple[str, float]]:
        """Rerank candidates using scoring model with graceful degradation fallback."""
        if not candidates:
            return []

        try:
            # Simulate reranker model scoring (e.g. cross-encoder or Cohere/Voyage rerank)
            reranked: list[tuple[str, float]] = []
            for doc_id, base_score in candidates:
                # Mock cross-encoder affinity score calculation
                affinity = base_score * 1.2
                if affinity >= self.score_threshold:
                    reranked.append((doc_id, float(affinity)))

            reranked.sort(key=lambda x: x[1], reverse=True)
            return reranked

        except Exception as exc:
            logger.warning("Reranker failed with exception: %s. Falling back to RRF order.", exc)
            if self.enable_fallback:
                return candidates
            raise
