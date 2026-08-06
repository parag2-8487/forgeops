# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.ai.embeddings import EmbeddingOrchestrator

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

async def test_table_selection_d48():
    voyage_orch = EmbeddingOrchestrator(backend="voyage")
    assert voyage_orch.get_target_table() == "embeddings_voyage"

    local_orch = EmbeddingOrchestrator(backend="bge_m3")
    assert local_orch.get_target_table() == "embeddings_local"

async def test_embedding_generation_local_fallback():
    orch = EmbeddingOrchestrator(backend="bge_m3")
    res = await orch.generate_embedding("def hello(): pass")
    assert res.dimensions == 1024
    assert len(res.vector) == 1024
    assert res.table == "embeddings_local"
    assert res.backend == "bge_m3"
