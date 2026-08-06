# SPDX-License-Identifier: FSL-1.1-ALv2
"""Redis-backed BM25 sparse index for hybrid code search (Leaf 11.10)."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_len: dict[str, int] = {}
        self.doc_freqs: dict[str, Counter[str]] = {}
        self.df: Counter[str] = Counter()
        self.avgdl: float = 0.0
        self.total_docs: int = 0

    def add_document(self, doc_id: str, text: str) -> None:
        tokens = re.findall(r"\w+", text.lower())
        self.doc_len[doc_id] = len(tokens)
        counts = Counter(tokens)
        self.doc_freqs[doc_id] = counts

        for token in counts:
            self.df[token] += 1

        self.total_docs += 1
        self.avgdl = sum(self.doc_len.values()) / max(self.total_docs, 1)

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        query_tokens = re.findall(r"\w+", query.lower())
        scores: dict[str, float] = {}

        for doc_id, counts in self.doc_freqs.items():
            score = 0.0
            doc_l = self.doc_len[doc_id]
            for token in query_tokens:
                if token not in counts:
                    continue
                freq = counts[token]
                df_val = self.df.get(token, 0)
                # Standard BM25 IDF formula
                idf = math.log((self.total_docs - df_val + 0.5) / (df_val + 0.5) + 1.0)
                numerator = freq * (self.k1 + 1.0)
                denominator = freq + self.k1 * (1.0 - self.b + self.b * (doc_l / max(self.avgdl, 1.0)))
                score += idf * (numerator / max(denominator, 1e-6))

            if score > 0:
                scores[doc_id] = score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]
