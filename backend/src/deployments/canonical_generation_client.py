# SPDX-License-Identifier: FSL-1.1-ALv2
"""Client for in-process streaming invocation of canonical Readiness and Generation endpoints."""

from __future__ import annotations

import json
import logging
import uuid
import inspect
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.sse import SSEEventType

logger = logging.getLogger(__name__)


class GenerationStreamError(RuntimeError):
    """Raised when canonical generation fails or emits an error event."""

    pass


@dataclass
class CanonicalGenerationResult:
    success: bool
    generation_run_id: uuid.UUID
    change_set_id: uuid.UUID | None
    files: list[str] = field(default_factory=list)
    completion_tokens: int = 0
    change_set_status: str | None = None
    outcome: str | None = None


async def consume_generation_sse_stream(
    stream: AsyncGenerator[bytes, None],
    *,
    heartbeat_pulse: Callable[[], None] | None = None,
) -> CanonicalGenerationResult:
    """Consumes the canonical 7-member SSE event stream emitted by POST /api/v1/generation/runs."""
    terminal_received = False
    result: CanonicalGenerationResult | None = None

    buffer = ""
    async for chunk in stream:
        if heartbeat_pulse:
            try:
                if inspect.iscoroutinefunction(heartbeat_pulse):
                    await heartbeat_pulse()
                else:
                    res = heartbeat_pulse()
                    if inspect.iscoroutine(res):
                        await res
            except Exception:
                pass
        buffer += chunk.decode("utf-8", errors="replace")

        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            lines = frame.strip().splitlines()
            event_name = ""
            data_str = ""
            for line in lines:
                if line.startswith("event:"):
                    event_name = line.removeprefix("event:").strip()
                elif line.startswith("data:"):
                    data_str = line.removeprefix("data:").strip()

            if not event_name or not data_str:
                continue

            try:
                payload = json.loads(data_str)
            except Exception as exc:
                raise GenerationStreamError(f"Malformed JSON in SSE data: {data_str}") from exc

            if event_name == SSEEventType.ERROR.value:
                terminal_received = True
                detail = payload.get("detail") or payload.get("state") or "Canonical generation failed"
                raise GenerationStreamError(str(detail))

            if event_name == SSEEventType.COMPLETE.value:
                terminal_received = True
                cs_id_raw = payload.get("change_set_id")
                gen_id_raw = payload.get("run_id")
                result = CanonicalGenerationResult(
                    success=True,
                    generation_run_id=uuid.UUID(str(gen_id_raw)),
                    change_set_id=uuid.UUID(str(cs_id_raw)) if cs_id_raw else None,
                    files=payload.get("files") or [],
                    completion_tokens=int(payload.get("completion_tokens") or 0),
                    change_set_status=payload.get("change_set_status"),
                    outcome=payload.get("outcome"),
                )
                break

    if not terminal_received or result is None:
        raise GenerationStreamError("Stream ended prematurely without a terminal complete or error event.")

    return result


async def stream_canonical_generation(
    client: httpx.AsyncClient,
    *,
    project_id: uuid.UUID,
    strategy: str,
    prompt: str = "Generate deployment artifacts",
    target_checks: list[str] | None = None,
    heartbeat_pulse: Callable[[], None] | None = None,
    headers: dict[str, str] | None = None,
) -> CanonicalGenerationResult:
    """Streams a generation request against canonical POST /api/v1/generation/runs."""
    payload: dict[str, Any] = {
        "project_id": str(project_id),
        "prompt": prompt,
        "target_strategy": strategy,
    }
    if target_checks:
        payload["target_checks"] = target_checks

    req_headers = {"Accept": "text/event-stream"}
    if headers:
        req_headers.update(headers)

    async with client.stream(
        "POST",
        "/api/v1/generation/runs",
        json=payload,
        headers=req_headers,
        timeout=httpx.Timeout(300.0, connect=10.0),
    ) as response:
        if response.status_code != 200:
            error_body = await response.aread()
            raise GenerationStreamError(
                f"Generation request returned HTTP {response.status_code}: {error_body.decode('utf-8', errors='replace')}"
            )
        return await consume_generation_sse_stream(response.aiter_bytes(), heartbeat_pulse=heartbeat_pulse)


async def fetch_canonical_readiness(
    client: httpx.AsyncClient,
    *,
    project_id: uuid.UUID,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetches canonical readiness report via GET /api/v1/projects/{id}/readiness."""
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    response = await client.get(
        f"/api/v1/projects/{project_id}/readiness",
        headers=req_headers,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    if response.status_code != 200:
        raise GenerationStreamError(
            f"Readiness check failed with HTTP {response.status_code}: {response.text}"
        )
    return response.json()
