# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.core.rrf import reciprocal_rank_fusion

pytestmark = [pytest.mark.mandatory]


def test_rrf_rank_fusion():
    bm25 = ["doc1", "doc2", "doc3"]
    vector = ["doc2", "doc4", "doc1"]

    results = reciprocal_rank_fusion(bm25, vector, k=60, top_n=5)
    doc_ids = [r[0] for r in results]

    # doc1 and doc2 appear in both rank lists, so they score highest
    assert "doc1" in doc_ids[:2]
    assert "doc2" in doc_ids[:2]


def test_rrf_score_calculation():
    bm25 = ["docA"]
    vector = ["docA"]
    results = reciprocal_rank_fusion(bm25, vector, k=60)
    expected_score = (1.0 / 61.0) + (1.0 / 61.0)
    assert abs(results[0][1] - expected_score) < 1e-6
