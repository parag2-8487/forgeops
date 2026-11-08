# SPDX-License-Identifier: FSL-1.1-ALv2
"""Live deployment logs over SSE. §2.2's `log` event type.

The agent already publishes `command.progress` on a per-command Redis channel; what was missing was a way
for a browser to reach one deployment's stream. `change_sets.command_id` (revision `0026`) supplies the
correlation, and this route subscribes to that channel and re-emits each frame as a `log` event.

An SSE stream is a VIEW, never a source of truth: the deployment's real state is its row, so a dropped
frame costs a smoother log and nothing else. That is why this route never writes anything.

The stream ENDS when the deployment settles, and says which way it settled. A stream that hung open after
a deployment finished would leave a spinner running over a completed rollout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..core.db import get_session
from ..core.errors import problem
from ..core.sse import SSE_MEDIA_TYPE, SSEEventType, format_event

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/deployments",
    tags=["deployments"],
    dependencies=[Depends(require_principal)],
)

#: Must match `websocket.hub._PROGRESS_CHANNEL`. Duplicated rather than imported because `hub` is a
#: transport and a route importing it would invert the dependency; the two are held together by
#: `test_deployment_logs.py`, which asserts the strings are equal.
PROGRESS_CHANNEL = "forgeops:sse:command:{command_id}"

#: How long to wait for a frame before emitting a keep-alive comment. Proxies drop an idle connection and
#: a deployment can legitimately be silent for minutes while an image pulls.
_IDLE_SECONDS = 15.0

#: The whole stream's bound. A deployment's own operation timeout is 15 minutes, so a stream outliving
#: that is watching something that can no longer be running.
_MAX_STREAM_SECONDS = 20 * 60


async def _deployment_row(session: AsyncSession, project_id: uuid.UUID, deployment_id: uuid.UUID) -> Any:
    result = await session.execute(
        text(
            "SELECT d.id, d.status, d.change_set_id, c.command_id "
            "FROM deployments d LEFT JOIN change_sets c ON c.id = d.change_set_id "
            "WHERE d.id = :id AND d.project_id = :project"
        ),
        {"id": deployment_id, "project": project_id},
    )
    return result.mappings().first()


@router.get(
    "/{deployment_id}/logs",
    response_class=StreamingResponse,
    responses={200: {"content": {SSE_MEDIA_TYPE: {}}}},
    summary="Live deployment output as SSE `log` events",
)
async def deployment_logs(
    project_id: uuid.UUID,
    deployment_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    """Stream one deployment's agent output.

    Refuses before opening a stream when the deployment has not been delivered: a stream that opened on a
    `pending_approval` deployment would show an empty log, and an operator would read that as "the
    deployment is doing nothing" rather than "a human has not approved it yet".
    """
    row = await _deployment_row(session, project_id, deployment_id)
    if row is None:
        raise problem(
            "deployment-absent",
            detail=f"no deployment {deployment_id} belongs to project {project_id}",
        )
    command_id = row["command_id"]
    if not command_id:
        raise problem(
            "deployment-invalid",
            detail=(
                f"deployment {deployment_id} is {row['status']} and has not been delivered to an agent, "
                "so it has no output yet. An empty stream would read as 'nothing is happening' rather "
                "than 'this is waiting'."
            ),
        )

    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise problem(
            "dependency-unavailable",
            detail="no Redis client is composed, so agent progress cannot be subscribed to",
        )

    async def stream() -> AsyncGenerator[bytes]:
        pubsub = redis.pubsub()
        await pubsub.subscribe(PROGRESS_CHANNEL.format(command_id=command_id))
        # The status is sent FIRST, so a client that connects mid-deployment knows where it joined rather
        # than inferring it from the first log line.
        yield format_event(SSEEventType.STATUS, {"deployment_id": str(deployment_id), "status": row["status"]}).encode()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _MAX_STREAM_SECONDS
        try:
            while loop.time() < deadline:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=_IDLE_SECONDS)
                if message is None:
                    # A comment frame, not a `log` event: a keep-alive that looked like output would put
                    # blank lines in an operator's log every fifteen seconds.
                    yield b": keep-alive\n\n"
                    continue
                raw = message.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    # Unparsable frames are reported as errors rather than dropped: silence here would be
                    # indistinguishable from a deployment that produced no output.
                    yield format_event(SSEEventType.ERROR, {"detail": "an unreadable progress frame arrived"}).encode()
                    continue
                yield format_event(SSEEventType.LOG, payload.get("data", payload)).encode()
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.close()
        # WHICH WAY IT SETTLED, read fresh rather than remembered from the top of the stream.
        settled = await _deployment_row(session, project_id, deployment_id)
        yield format_event(
            SSEEventType.COMPLETE,
            {"deployment_id": str(deployment_id), "status": settled["status"] if settled else "unknown"},
        ).encode()

    return StreamingResponse(
        stream(),
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
