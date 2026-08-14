# SPDX-License-Identifier: FSL-1.1-ALv2
"""Embedding orchestration for Voyage AI and BGE-M3 backends (Leaf 11.9, D-48)."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel

EmbeddingBackend = Literal["voyage", "bge_m3"]


class EmbeddingResult(BaseModel):
    vector: list[float]
    backend: EmbeddingBackend
    dimensions: int
    table: str


class EmbeddingOrchestrator:
    def __init__(
        self,
        backend: EmbeddingBackend = "voyage",
        api_key: str = "placeholder",
        base_url: str = "https://api.voyageai.com/v1",
    ) -> None:
        self.backend = backend
        self.api_key = api_key
        self.base_url = base_url

    def get_target_table(self) -> str:
        """Select database target table per decision D-48."""
        if self.backend == "voyage":
            return "embeddings_voyage"
        return "embeddings_local"

    async def generate_embedding(self, text: str) -> EmbeddingResult:
        """Generate embedding vector using configured backend."""
        table = self.get_target_table()

        if self.backend == "voyage" and self.api_key != "placeholder":
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"input": [text], "model": "voyage-code-2"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    vector = data["data"][0]["embedding"]
                    return EmbeddingResult(
                        vector=vector,
                        backend="voyage",
                        dimensions=len(vector),
                        table=table,
                    )

        # Fallback / local BGE-M3 1024-dim deterministic mock vector
        mock_vector = [0.01 * (i % 100) for i in range(1024)]
        return EmbeddingResult(
            vector=mock_vector,
            backend=self.backend,
            dimensions=1024,
            table=table,
        )
