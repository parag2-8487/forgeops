# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generation service with Server-Sent Events (SSE) streaming (Leaf 13.8)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator

from pydantic import BaseModel


class RunRecord(BaseModel):
    run_id: uuid.UUID
    project_id: uuid.UUID
    status: str
    token_usage: int


class GenerationService:
    async def stream_generation(self, project_id: uuid.UUID, prompt: str) -> AsyncGenerator[str]:
        """Stream generation progress and events via SSE standard format."""
        run_id = uuid.uuid4()
        yield f"event: run_start\ndata: {json.dumps({'run_id': str(run_id), 'project_id': str(project_id)})}\n\n"

        # Stream token chunks
        chunks = ["Generating ", "DevOps ", "manifests..."]
        for chunk in chunks:
            yield f"event: token_chunk\ndata: {json.dumps({'chunk': chunk})}\n\n"

        yield f"event: run_complete\ndata: {json.dumps({'run_id': str(run_id), 'status': 'success'})}\n\n"
