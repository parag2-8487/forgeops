# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test Q-26: SSE stream well-formedness (Leaf 13.12)."""

import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st
from src.generation.service import GenerationService

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


@given(prompt=st.text(min_size=1, max_size=100))
async def test_property_q26_sse_stream_well_formedness(prompt: str):
    service = GenerationService()
    pid = uuid.uuid4()

    async for event_str in service.stream_generation(pid, prompt):
        assert event_str.startswith("event: ")
        assert "\ndata: " in event_str
        assert event_str.endswith("\n\n")
