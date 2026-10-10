# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous Deployment Orchestrator HTTP & Streaming Surface (Stage 7).

Reference:
- Specification: docs/superpowers/specs/2026-10-10-autonomous-deployment-orchestrator-design.md (§5.1, §5.2)
- Plan: docs/superpowers/plans/2026-10-10-autonomous-deployment-orchestrator.md (Stage 7)

Provides REST endpoints, Server-Sent Events (SSE) streaming, and WebSocket streaming:
1. `POST /api/v1/projects/{project_id}/autonomous-deploy`: Create run (201 new, 200 idempotent repeat, 409 conflict).
2. `GET /api/v1/projects/{project_id}/autonomous-deploy/{run_id}`: Authoritative snapshot with agent pairing status.
3. `POST /api/v1/projects/{project_id}/autonomous-deploy/{run_id}/start`:
   Initiate execution (requires paired agent heartbeat <= 30s).
4. `POST /api/v1/projects/{project_id}/autonomous-deploy/{run_id}/cancel`:
   Request cancellation (settles pending/running).
5. `POST /api/v1/projects/{project_id}/autonomous-deploy/{run_id}/retry`:
   Create immutable attempt for failed/rolled-back run.
6. `GET /api/v1/projects/{project_id}/autonomous-deploy/{run_id}/logs`: Cursor-paginated logs.
7. `GET /api/v1/projects/{project_id}/autonomous-deploy/{run_id}/events`:
   SSE stream with outbox replay and live Redis events.
8. `WebSocket /api/v1/projects/{project_id}/autonomous-deploy/{run_id}/ws`: WebSocket stream with cookie/header auth.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketState

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import ProblemException, problem
from ..core.sse import SSE_MEDIA_TYPE, SSEEventType, format_event
from .autonomous_models import (
    AutonomousDeploymentOutbox,
)
from .autonomous_outbox import AutonomousOutboxPublisher, get_autonomous_event_channel
from .autonomous_recovery import CancellationSettlement
from .autonomous_schemas import (
    AutonomousRunPublicResponse,
    CreateAutonomousRunRequest,
    LogEntryPublicResponse,
    PaginatedLogsResponse,
)
from .autonomous_service import AutonomousDeploymentService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/autonomous-deploy",
    tags=["autonomous-deploy"],
    dependencies=[Depends(require_principal)],
)

_IDLE_KEEP_ALIVE_SECONDS = 15.0
_MAX_STREAM_TIMEOUT_SECONDS = 3600.0


async def _check_agent_connected(
    session: AsyncSession,
    request_or_app_state: Any,
    project_id: uuid.UUID,
) -> bool:
    """Inspects active agent device status and ensures heartbeat was recorded within 30 seconds."""
    device_service = getattr(request_or_app_state, "device_service", None)
    if device_service is None and hasattr(request_or_app_state, "app"):
        device_service = getattr(request_or_app_state.app.state, "device_service", None)
    if device_service is None and hasattr(request_or_app_state, "state"):
        device_service = getattr(request_or_app_state.state, "device_service", None)

    if device_service is not None and hasattr(device_service, "active_device_for"):
        active_device = await device_service.active_device_for(session, project_id)
        if active_device is None or getattr(active_device, "last_seen", None) is None:
            return False
        now = datetime.now(UTC)
        last_seen = active_device.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        delta = (now - last_seen).total_seconds()
        return delta <= 30.0

    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT last_seen FROM agent_devices WHERE project_id = :project AND status = 'active' "
            "ORDER BY last_seen DESC NULLS LAST, created_at DESC LIMIT 1"
        ),
        {"project": project_id},
    )
    row = result.first()
    if row is None or row[0] is None:
        return False
    now = datetime.now(UTC)
    last_seen = row[0]
    if isinstance(last_seen, str):
        last_seen = datetime.fromisoformat(last_seen)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    delta = (now - last_seen).total_seconds()
    return delta <= 30.0


def _map_event_type_to_sse(event_type: str) -> SSEEventType:
    """Map outbox event types to the canonical closed SSEEventType vocabulary."""
    norm = event_type.lower()
    if norm in ("run_created", "run_started"):
        return SSEEventType.STATUS
    if norm in ("stage_transition", "stage_started", "stage_completed", "progress_updated"):
        return SSEEventType.PROGRESS
    if norm in ("log", "log_emitted"):
        return SSEEventType.LOG
    if norm in ("run_succeeded", "run_completed"):
        return SSEEventType.COMPLETE
    if norm in ("run_failed", "run_cancelled", "error"):
        return SSEEventType.ERROR
    return SSEEventType.STATUS


@router.post(
    "",
    response_model=AutonomousRunPublicResponse,
    summary="Create or retrieve an autonomous deployment run (idempotent)",
)
async def create_run(
    project_id: uuid.UUID,
    body: CreateAutonomousRunRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Create a new autonomous deployment run or return an existing run if idempotent."""
    service = AutonomousDeploymentService()
    run, is_created = await service.create_run(
        session,
        project_id=project_id,
        requested_by=principal.user_id,
        request=body,
        tenant_id=principal.tenant_id,
        link_service=getattr(request.app.state, "github_link_service", None),
    )

    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        publisher = AutonomousOutboxPublisher()
        with contextlib.suppress(Exception):
            await publisher.drain_run_events(session, redis, run.id)

    agent_connected = await _check_agent_connected(session, request.app.state, project_id)
    resp = AutonomousRunPublicResponse.model_validate(run)
    resp.agent_connected = agent_connected

    status_code = status.HTTP_201_CREATED if is_created else status.HTTP_200_OK
    return JSONResponse(
        status_code=status_code,
        content=json.loads(resp.model_dump_json(by_alias=True)),
    )


@router.get(
    "/{run_id}",
    response_model=AutonomousRunPublicResponse,
    summary="Get authoritative run snapshot and stages",
)
async def get_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AutonomousRunPublicResponse:
    """Fetch authoritative autonomous run record, stages, and agent connectivity."""
    service = AutonomousDeploymentService()
    run = await service.get_run(session, project_id=project_id, run_id=run_id)
    if run is None:
        raise problem(
            "deployment-absent",
            detail=f"Autonomous deployment run '{run_id}' not found in project '{project_id}'.",
        )

    agent_connected = await _check_agent_connected(session, request.app.state, project_id)
    resp = AutonomousRunPublicResponse.model_validate(run)
    resp.agent_connected = agent_connected
    return resp


@router.post(
    "/{run_id}/start",
    response_model=AutonomousRunPublicResponse,
    summary="Initiate execution of a pending autonomous deployment run",
)
async def start_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AutonomousRunPublicResponse:
    """Initiate execution of a pending run.

    Enforces:
    - Active agent is connected with heartbeat <= 30s (412 Precondition Failed if not).
    - Status must be 'pending' (or returns idempotent 200 OK if already 'running').
    - Dispatches execution task to background task dispatcher.
    """
    service = AutonomousDeploymentService()
    run = await service.get_run(session, project_id=project_id, run_id=run_id)
    if run is None:
        raise problem(
            "deployment-absent",
            detail=f"Autonomous deployment run '{run_id}' not found in project '{project_id}'.",
        )

    # Idempotent repeat: if already running, return 200 OK immediately
    if run.status == "running":
        agent_connected = await _check_agent_connected(session, request.app.state, project_id)
        resp = AutonomousRunPublicResponse.model_validate(run)
        resp.agent_connected = agent_connected
        return resp

    if run.status != "pending":
        raise problem(
            "autonomous-run-conflict",
            detail=(
                f"Cannot start autonomous deployment run '{run_id}' in status '{run.status}'. "
                "Only runs in 'pending' status can be started."
            ),
        )

    agent_connected = await _check_agent_connected(session, request.app.state, project_id)
    if not agent_connected:
        raise ProblemException(
            status=412,
            type_suffix="agent-disconnected",
            title="Precondition Failed",
            detail=(
                f"Starting deployment requires an active paired agent with a recent heartbeat "
                f"(<= 30s) for project '{project_id}'."
            ),
        )

    now = datetime.now(UTC)
    run.status = "running"
    run.started_at = now
    run.dispatch_status = "dispatching"
    run.dispatch_requested_at = now

    run.outbox_sequence_counter += 1
    outbox = AutonomousDeploymentOutbox(
        run_id=run.id,
        event_seq=run.outbox_sequence_counter,
        event_type="run_started",
        payload={
            "run_id": str(run.id),
            "project_id": str(run.project_id),
            "status": run.status,
            "started_at": now.isoformat(),
        },
        status="pending",
    )
    session.add(outbox)
    session.add(run)
    await session.flush()

    dispatcher = getattr(request.app.state, "task_dispatcher", None)
    if dispatcher is not None:
        try:
            await dispatcher.enqueue(
                "autonomous_deploy_run",
                {"run_id": str(run.id), "project_id": str(project_id)},
            )
            run.dispatch_status = "enqueued"
            await session.flush()
        except Exception:
            logger.warning("Failed to enqueue autonomous_deploy_run for run %s", run.id, exc_info=True)

    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        publisher = AutonomousOutboxPublisher()
        with contextlib.suppress(Exception):
            await publisher.drain_run_events(session, redis, run.id)

    resp = AutonomousRunPublicResponse.model_validate(run)
    resp.agent_connected = True
    return resp


@router.post(
    "/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request cooperative cancellation of an autonomous deployment run",
)
async def cancel_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Request cancellation.

    If pending: settles immediately to 'cancelled'.
    If running: marks 'cancelling' and signals worker settlement.
    Returns 202 Accepted.
    """
    service = AutonomousDeploymentService()
    run = await service.get_run(session, project_id=project_id, run_id=run_id)
    if run is None:
        raise problem(
            "deployment-absent",
            detail=f"Autonomous deployment run '{run_id}' not found in project '{project_id}'.",
        )

    now = datetime.now(UTC)
    if run.status == "pending":
        run.status = "cancelled"
        run.completed_at = now
        run.outbox_sequence_counter += 1
        outbox = AutonomousDeploymentOutbox(
            run_id=run.id,
            event_seq=run.outbox_sequence_counter,
            event_type="run_cancelled",
            payload={"run_id": str(run.id), "status": "cancelled", "completed_at": now.isoformat()},
            status="pending",
        )
        session.add(outbox)
        session.add(run)
        await session.flush()
    elif run.status == "running":
        run.status = "cancelling"
        run.outbox_sequence_counter += 1
        outbox = AutonomousDeploymentOutbox(
            run_id=run.id,
            event_seq=run.outbox_sequence_counter,
            event_type="run_cancelling",
            payload={"run_id": str(run.id), "status": "cancelling"},
            status="pending",
        )
        session.add(outbox)
        session.add(run)
        settlement = CancellationSettlement()
        with contextlib.suppress(Exception):
            await settlement.terminate_processes(run.id, grace_period_seconds=0.1)
    elif run.status in ("cancelling", "cancelled"):
        pass  # Already cancelling or cancelled
    else:
        raise problem(
            "autonomous-run-conflict",
            detail=f"Cannot cancel run in terminal status '{run.status}'.",
        )

    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        publisher = AutonomousOutboxPublisher()
        with contextlib.suppress(Exception):
            await publisher.drain_run_events(session, redis, run.id)

    agent_connected = await _check_agent_connected(session, request.app.state, project_id)
    resp = AutonomousRunPublicResponse.model_validate(run)
    resp.agent_connected = agent_connected

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=json.loads(resp.model_dump_json(by_alias=True)),
    )


@router.post(
    "/{run_id}/retry",
    response_model=AutonomousRunPublicResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new immutable attempt for a failed or rolled-back run",
)
async def retry_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Create an immutable retry run chaining with parent_run_id and incremented attempt_number."""
    service = AutonomousDeploymentService()
    retry = await service.retry_run(
        session,
        project_id=project_id,
        run_id=run_id,
        requested_by=principal.user_id,
    )

    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        publisher = AutonomousOutboxPublisher()
        with contextlib.suppress(Exception):
            await publisher.drain_run_events(session, redis, retry.id)

    agent_connected = await _check_agent_connected(session, request.app.state, project_id)
    resp = AutonomousRunPublicResponse.model_validate(retry)
    resp.agent_connected = agent_connected

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=json.loads(resp.model_dump_json(by_alias=True)),
    )


@router.get(
    "/{run_id}/logs",
    response_model=PaginatedLogsResponse,
    summary="Cursor-paginated log retrieval strictly ordered by monotonic log_seq ASC",
)
async def get_logs(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    since_log_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
    stage_name: str | None = Query(default=None),
) -> PaginatedLogsResponse:
    """Fetch logs starting after `since_log_seq` up to `limit` entries."""
    service = AutonomousDeploymentService()
    run = await service.get_run(session, project_id=project_id, run_id=run_id)
    if run is None:
        raise problem(
            "deployment-absent",
            detail=f"Autonomous deployment run '{run_id}' not found in project '{project_id}'.",
        )

    logs, has_more, next_log_seq = await service.get_logs(
        session,
        run_id=run_id,
        since_log_seq=since_log_seq,
        limit=limit,
        stage_name=stage_name,
    )

    log_entries = [LogEntryPublicResponse.model_validate(entry) for entry in logs]
    return PaginatedLogsResponse(
        run_id=run_id,
        logs=log_entries,
        has_more=has_more,
        next_log_seq=next_log_seq,
        total_lines=run.log_sequence_counter,
    )


@router.get(
    "/{run_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {SSE_MEDIA_TYPE: {}}}},
    summary="Real-time Server-Sent Events (SSE) streaming with outbox replay and live Redis relay",
)
async def stream_events(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    since_event_seq: int = Query(default=0, ge=0),
    max_events: int | None = Query(default=None, ge=1),
) -> StreamingResponse:
    """Stream outbox events over SSE using the subscribe-before-replay protocol."""
    service = AutonomousDeploymentService()
    run = await service.get_run(session, project_id=project_id, run_id=run_id)
    if run is None:
        raise problem(
            "deployment-absent",
            detail=f"Autonomous deployment run '{run_id}' not found in project '{project_id}'.",
        )

    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise problem(
            "dependency-unavailable",
            detail="Redis client is not configured; real-time event streaming is unavailable.",
        )

    async def sse_generator() -> AsyncGenerator[bytes]:
        channel = get_autonomous_event_channel(run_id)
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)

        # 1. Determine PostgreSQL high-water mark
        hwm_stmt = select(func.max(AutonomousDeploymentOutbox.event_seq)).where(
            AutonomousDeploymentOutbox.run_id == run_id
        )
        hwm_result = await session.execute(hwm_stmt)
        high_water_mark = hwm_result.scalar() or 0

        # 2. Replay historical events from PostgreSQL
        replay_stmt = (
            select(AutonomousDeploymentOutbox)
            .where(
                AutonomousDeploymentOutbox.run_id == run_id,
                AutonomousDeploymentOutbox.event_seq > since_event_seq,
                AutonomousDeploymentOutbox.event_seq <= high_water_mark,
            )
            .order_by(AutonomousDeploymentOutbox.event_seq.asc())
        )
        replay_result = await session.execute(replay_stmt)
        historical_events = list(replay_result.scalars().all())

        seen_event_seq = since_event_seq
        emitted_count = 0
        for ev in historical_events:
            sse_type = _map_event_type_to_sse(ev.event_type)
            data_dict = {
                "event_seq": ev.event_seq,
                "event_type": ev.event_type,
                "payload": ev.payload,
                "run_id": str(ev.run_id),
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
            }
            yield format_event(sse_type, data_dict).encode("utf-8")
            seen_event_seq = max(seen_event_seq, ev.event_seq)
            emitted_count += 1
            if max_events is not None and emitted_count >= max_events:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
                return

        # 3. Stream live events from Redis pub/sub
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _MAX_STREAM_TIMEOUT_SECONDS

        try:
            while loop.time() < deadline:
                if await request.is_disconnected():
                    break

                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=_IDLE_KEEP_ALIVE_SECONDS,
                )
                if message is None:
                    yield b": keep-alive\n\n"
                    continue

                raw_data = message.get("data")
                if isinstance(raw_data, bytes):
                    raw_data = raw_data.decode("utf-8", "replace")

                try:
                    event_data = json.loads(raw_data)
                except (TypeError, ValueError):
                    yield format_event(
                        SSEEventType.ERROR,
                        {"detail": "Unreadable event frame received from message broker."},
                    ).encode("utf-8")
                    continue

                event_seq = event_data.get("event_seq", 0)
                if event_seq <= seen_event_seq:
                    continue  # Deduplicate already replayed frame

                seen_event_seq = event_seq
                ev_type = event_data.get("event_type", "status")
                sse_type = _map_event_type_to_sse(ev_type)
                yield format_event(sse_type, event_data).encode("utf-8")
                emitted_count += 1

                if max_events is not None and emitted_count >= max_events:
                    break

                if ev_type in ("run_succeeded", "run_failed", "run_cancelled", "run_completed"):
                    break
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel)
                await pubsub.close()

    return StreamingResponse(
        sse_generator(),
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/{run_id}/ws")
async def websocket_events(
    websocket: WebSocket,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    since_event_seq: int = Query(default=0, ge=0),
) -> None:
    """WebSocket streaming endpoint replaying missed outbox events and relaying live events."""
    service = AutonomousDeploymentService()
    run = await service.get_run(session, project_id=project_id, run_id=run_id)
    if run is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Run not found")
        return

    redis = getattr(websocket.app.state, "redis", None)
    if redis is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Redis unavailable")
        return

    await websocket.accept()

    channel = get_autonomous_event_channel(run_id)
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    # 1. High-water mark query
    hwm_stmt = select(func.max(AutonomousDeploymentOutbox.event_seq)).where(AutonomousDeploymentOutbox.run_id == run_id)
    hwm_result = await session.execute(hwm_stmt)
    high_water_mark = hwm_result.scalar() or 0

    # 2. Replay historical events
    replay_stmt = (
        select(AutonomousDeploymentOutbox)
        .where(
            AutonomousDeploymentOutbox.run_id == run_id,
            AutonomousDeploymentOutbox.event_seq > since_event_seq,
            AutonomousDeploymentOutbox.event_seq <= high_water_mark,
        )
        .order_by(AutonomousDeploymentOutbox.event_seq.asc())
    )
    replay_result = await session.execute(replay_stmt)
    historical_events = list(replay_result.scalars().all())

    seen_event_seq = since_event_seq
    for ev in historical_events:
        data_dict = {
            "event_seq": ev.event_seq,
            "event_type": ev.event_type,
            "payload": ev.payload,
            "run_id": str(ev.run_id),
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        }
        await websocket.send_json(data_dict)
        seen_event_seq = max(seen_event_seq, ev.event_seq)

    # 3. Stream live frames
    async def listen_redis() -> None:
        nonlocal seen_event_seq
        while websocket.client_state == WebSocketState.CONNECTED:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=_IDLE_KEEP_ALIVE_SECONDS,
            )
            if message is None:
                await websocket.send_json({"event_type": "ping", "timestamp": datetime.now(UTC).isoformat()})
                continue

            raw_data = message.get("data")
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode("utf-8", "replace")

            try:
                event_data = json.loads(raw_data)
            except (TypeError, ValueError):
                continue

            event_seq = event_data.get("event_seq", 0)
            if event_seq <= seen_event_seq:
                continue

            seen_event_seq = event_seq
            await websocket.send_json(event_data)

            ev_type = event_data.get("event_type", "")
            if ev_type in ("run_succeeded", "run_failed", "run_cancelled", "run_completed"):
                break

    async def listen_client() -> None:
        while websocket.client_state == WebSocketState.CONNECTED:
            try:
                # Discard or handle client pings / ack frames
                _ = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                break

    redis_task = asyncio.create_task(listen_redis())
    client_task = asyncio.create_task(listen_client())

    done, pending = await asyncio.wait(
        [redis_task, client_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with contextlib.suppress(Exception):
        await pubsub.unsubscribe(channel)
        await pubsub.close()

    if websocket.client_state == WebSocketState.CONNECTED:
        await websocket.close()
