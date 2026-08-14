# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.analysis.bm25 import BM25Index

pytestmark = [pytest.mark.mandatory]


def test_bm25_indexing_and_search():
    idx = BM25Index()
    idx.add_document("doc1", "def parse_ast(): return True")
    idx.add_document("doc2", "func NewParser() *Parser")
    idx.add_document("doc3", "console.log('hello world')")

    results = idx.search("parse_ast", top_k=5)
    assert len(results) >= 1
    assert results[0][0] == "doc1"
    assert results[0][1] > 0.0


def test_bm25_ranking_relevance():
    idx = BM25Index()
    idx.add_document("doc1", "python python python code")
    idx.add_document("doc2", "python code")

    results = idx.search("python")
    assert len(results) == 2
    # doc1 has higher term frequency for python
    assert results[0][0] == "doc1"
