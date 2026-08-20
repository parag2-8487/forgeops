# SPDX-License-Identifier: FSL-1.1-ALv2
"""The generation stream's shape.

This test used to assert `event: run_start`, `event: token_chunk` and `event: run_complete` — the
three names the service emitted and §7.4 does not contain. That is why the divergence survived: the
only test of the stream encoded the divergence as the expected result, so the enum in `core/sse.py`
described a contract while the test pinned its violation.

Rewritten to assert the six-word vocabulary. `test_generation_api.py` covers the ordering and the
per-event payloads in more depth; this stays as the narrow check that the service's own frames are
well formed.
"""

import uuid

import pytest
from src.core.sse import SSEEventType
from src.generation.service import GenerationService

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


async def test_generation_sse_stream():
    service = GenerationService()
    project_id = uuid.uuid4()

    events = []
    async for event_str in service.stream_generation(project_id, "Create k8s deployment"):
        events.append(event_str)

    assert len(events) >= 3
    assert "event: status" in events[0]
    assert "event: token" in events[1]
    assert "event: complete" in events[-1]

    # Every frame names an event in §7.4's vocabulary and terminates with the blank line the SSE
    # grammar requires. `token` is a prefix of nothing else in the vocabulary, so the membership
    # check below is exact rather than incidental.
    allowed = {e.value for e in SSEEventType}
    for frame in events:
        assert frame.endswith("\n\n"), f"frame is not blank-line terminated: {frame!r}"
        name = frame.split("\n", 1)[0].removeprefix("event: ")
        assert name in allowed, f"{name!r} is not one of §7.4's six event types"
