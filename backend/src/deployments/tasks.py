# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous deployment worker tasks (Stage 6 / Stage 11)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from src.core.config import get_settings
from src.core.db import create_db_engine, create_sessionmaker
from src.core.tasks import register_task
from src.deployments.autonomous_worker import run_pipeline

logger = logging.getLogger(__name__)


@register_task("autonomous_deploy_run")
async def execute_autonomous_deploy_task(payload: dict[str, Any]) -> dict[str, Any]:
    """Worker task handler executing an autonomous deployment pipeline run."""
    run_id_raw = payload.get("run_id")
    if not run_id_raw:
        logger.error("execute_autonomous_deploy_task received payload without run_id: %s", payload)
        return {"status": "error", "message": "missing run_id"}

    run_id = uuid.UUID(str(run_id_raw))
    worker_id = payload.get("worker_id") or f"worker-{uuid.uuid4().hex[:8]}"

    settings = get_settings()
    engine = create_db_engine(settings)
    sessionmaker = create_sessionmaker(engine)

    redis_client: Any = None
    try:
        from redis.asyncio import from_url

        redis_client = from_url(str(settings.redis_url))
    except Exception:
        pass

    try:
        async with sessionmaker() as session:
            try:
                logger.info("Executing autonomous deployment pipeline for run %s on %s", run_id, worker_id)
                run = await run_pipeline(
                    session,
                    run_id=run_id,
                    worker_id=worker_id,
                    redis=redis_client,
                    sessionmaker=sessionmaker,
                )
                await session.commit()
                logger.info("Autonomous deployment pipeline for run %s finished with status %s", run_id, run.status)
                return {"status": run.status, "run_id": str(run.id), "progress_pct": run.progress_pct}
            except (Exception, asyncio.CancelledError) as exc:
                logger.exception("Error executing autonomous deployment pipeline for run %s: %s", run_id, exc)
                with contextlib.suppress(Exception):
                    await session.rollback()

                # Ensure run does NOT remain indefinitely stuck in 'running'
                try:
                    from datetime import UTC, datetime

                    from sqlalchemy import func, select, update
                    from src.deployments.autonomous_models import (
                        AutonomousDeployment,
                        AutonomousDeploymentLog,
                        AutonomousDeploymentOutbox,
                        AutonomousDeploymentStage,
                    )
                    from src.deployments.autonomous_outbox import AutonomousOutboxPublisher

                    fail_now = datetime.now(UTC)
                    is_cancelled = isinstance(exc, asyncio.CancelledError)
                    is_timeout = (
                        is_cancelled
                        or isinstance(exc, asyncio.TimeoutError | TimeoutError)
                        or "timeout" in str(exc).lower()
                    )
                    if is_cancelled:
                        error_msg = "Worker pipeline cancelled or timed out (ARQ Task Cancellation)"
                    elif is_timeout:
                        error_msg = f"Worker pipeline timed out: {type(exc).__name__}: {str(exc)}"
                    else:
                        error_msg = f"Worker pipeline failed: {type(exc).__name__}: {str(exc)}"

                    res = await session.execute(select(AutonomousDeployment).where(AutonomousDeployment.id == run_id))
                    db_run = res.scalars().first()

                    next_seq = 1
                    project_id_str = ""
                    if db_run is not None:
                        project_id_str = str(db_run.project_id)
                        # Idempotency: if run is already in terminal state, do not overwrite
                        if db_run.status in ("failed", "succeeded", "cancelled", "rolled_back"):
                            logger.info(
                                "Run %s is already terminal (%s); skipping failure transition", run_id, db_run.status
                            )
                            if is_cancelled:
                                raise
                            return {
                                "status": db_run.status,
                                "run_id": str(db_run.id),
                                "progress_pct": db_run.progress_pct,
                            }

                        # Worker fencing: check if another worker holds an active lease
                        if (
                            db_run.worker_id
                            and db_run.worker_id != worker_id
                            and db_run.lease_expires_at
                            and db_run.lease_expires_at > fail_now
                        ):
                            logger.warning(
                                "Run %s is held by another active worker %s until %s; skipping failure transition by %s",
                                run_id,
                                db_run.worker_id,
                                db_run.lease_expires_at,
                                worker_id,
                            )
                            if is_cancelled:
                                raise
                            return {
                                "status": db_run.status,
                                "run_id": str(db_run.id),
                                "progress_pct": db_run.progress_pct,
                            }

                        if db_run.status in ("pending", "running"):
                            db_run.status = "failed"
                            db_run.error_summary = error_msg
                            db_run.completed_at = fail_now

                        # Determine next event_seq dynamically from existing outbox records and counter
                        max_seq_res = await session.execute(
                            select(func.max(AutonomousDeploymentOutbox.event_seq)).where(
                                AutonomousDeploymentOutbox.run_id == run_id
                            )
                        )
                        current_max = max_seq_res.scalar() or db_run.outbox_sequence_counter or 0
                        next_seq = max(current_max, db_run.outbox_sequence_counter) + 1
                        db_run.outbox_sequence_counter = next_seq

                    # Append terminal failure log to database
                    if db_run is not None:
                        curr_stage = db_run.current_stage or "worker"
                        next_log_seq = (db_run.log_sequence_counter or 0) + 1
                        db_run.log_sequence_counter = next_log_seq
                        fail_log = AutonomousDeploymentLog(
                            run_id=run_id,
                            stage_name=curr_stage,
                            log_seq=next_log_seq,
                            level="ERROR",
                            message=error_msg,
                        )
                        session.add(fail_log)

                    # Mark any active stages as failed
                    stmt_stages = (
                        update(AutonomousDeploymentStage)
                        .where(
                            AutonomousDeploymentStage.run_id == run_id,
                            AutonomousDeploymentStage.status.in_(["running", "waiting"]),
                        )
                        .values(
                            status="failed",
                            error_message=error_msg,
                            completed_at=fail_now,
                        )
                    )
                    await session.execute(stmt_stages)

                    # Mark any pending unstarted stages as skipped so UI reflects pipeline halt
                    stmt_pending = (
                        update(AutonomousDeploymentStage)
                        .where(
                            AutonomousDeploymentStage.run_id == run_id,
                            AutonomousDeploymentStage.status == "pending",
                        )
                        .values(
                            status="skipped",
                            error_message=f"Pipeline terminated before stage execution: {error_msg}",
                            completed_at=fail_now,
                        )
                    )
                    await session.execute(stmt_pending)

                    # Emit terminal run_failed outbox event with consecutive sequence
                    outbox = AutonomousDeploymentOutbox(
                        run_id=run_id,
                        event_seq=next_seq,
                        event_type="run_failed",
                        payload={
                            "run_id": str(run_id),
                            "project_id": project_id_str,
                            "status": "failed",
                            "error_summary": error_msg,
                            "completed_at": fail_now.isoformat(),
                            "event_seq": next_seq,
                        },
                        status="pending",
                    )
                    session.add(outbox)
                    await session.commit()

                    if redis_client is not None:
                        with contextlib.suppress(Exception):
                            pub = AutonomousOutboxPublisher()
                            await pub.drain_run_events(session, redis_client, run_id)
                except Exception as cleanup_err:
                    logger.error("Failed to mark run %s as failed after worker error: %s", run_id, cleanup_err)
                raise
    finally:
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                pass
        await engine.dispose()
