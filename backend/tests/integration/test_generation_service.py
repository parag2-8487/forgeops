# SPDX-License-Identifier: FSL-1.1-ALv2
import uuid
import pytest
from src.generation.service import GenerationService

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

async def test_generation_sse_stream():
    service = GenerationService()
    project_id = uuid.uuid4()

    events = []
    async for event_str in service.stream_generation(project_id, "Create k8s deployment"):
        events.append(event_str)

    assert len(events) >= 3
    assert "event: run_start" in events[0]
    assert "event: token_chunk" in events[1]
    assert "event: run_complete" in events[-1]
