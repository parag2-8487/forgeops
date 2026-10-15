# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for canonical generation SSE stream consumer."""

import uuid

import pytest
from src.deployments.canonical_generation_client import (
    GenerationStreamError,
    consume_generation_sse_stream,
)


@pytest.mark.asyncio
async def test_consume_valid_sse_stream_success():
    gen_run_id = str(uuid.uuid4())
    change_set_id = str(uuid.uuid4())

    async def mock_stream():
        yield f'event: progress\ndata: {{"run_id":"{gen_run_id}","state":"requesting_model","attempt":1,"max_attempts":3,"tier":"tier_1_local"}}\n\n'.encode()
        yield f'event: token\ndata: {{"run_id":"{gen_run_id}","text":"FROM node:20-alpine\\n","attempt":1}}\n\n'.encode()
        yield f'event: validation\ndata: {{"run_id":"{gen_run_id}","passed":true,"findings":[],"served_from":"provider","attempt":1}}\n\n'.encode()
        yield f'event: complete\ndata: {{"run_id":"{gen_run_id}","state":"accepted","files":["Dockerfile","docker-compose.yml"],"completion_tokens":420,"change_set_id":"{change_set_id}","change_set_status":"pending_approval","outcome":"approval-required"}}\n\n'.encode()

    result = await consume_generation_sse_stream(mock_stream())
    assert result.success is True
    assert result.change_set_id == uuid.UUID(change_set_id)
    assert result.generation_run_id == uuid.UUID(gen_run_id)
    assert result.files == ["Dockerfile", "docker-compose.yml"]


@pytest.mark.asyncio
async def test_consume_sse_stream_error_terminal():
    gen_run_id = str(uuid.uuid4())

    async def mock_stream():
        yield f'event: error\ndata: {{"run_id":"{gen_run_id}","detail":"syntax validation refused","state":"failed","generated":[]}}\n\n'.encode()

    with pytest.raises(GenerationStreamError, match="syntax validation refused"):
        await consume_generation_sse_stream(mock_stream())


@pytest.mark.asyncio
async def test_consume_sse_stream_premature_eof():
    async def mock_stream():
        yield b'event: progress\ndata: {"state":"requesting_model"}\n\n'

    with pytest.raises(GenerationStreamError, match="Stream ended prematurely"):
        await consume_generation_sse_stream(mock_stream())


@pytest.mark.asyncio
async def test_consume_sse_stream_heartbeat_pulse():
    pulse_count = 0

    def pulse():
        nonlocal pulse_count
        pulse_count += 1

    gen_run_id = str(uuid.uuid4())
    change_set_id = str(uuid.uuid4())

    async def mock_stream():
        yield f'event: progress\ndata: {{"run_id":"{gen_run_id}"}}\n\n'.encode()
        yield f'event: complete\ndata: {{"run_id":"{gen_run_id}","change_set_id":"{change_set_id}","files":["Dockerfile"]}}\n\n'.encode()

    result = await consume_generation_sse_stream(mock_stream(), heartbeat_pulse=pulse)
    assert result.success is True
    assert pulse_count == 2
