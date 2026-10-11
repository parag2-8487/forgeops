# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous deployment worker tasks (Stage 6 / Stage 11)."""

from __future__ import annotations

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

    try:
        async with sessionmaker() as session:
            try:
                logger.info("Executing autonomous deployment pipeline for run %s on %s", run_id, worker_id)
                run = await run_pipeline(session, run_id=run_id, worker_id=worker_id)
                await session.commit()
                logger.info("Autonomous deployment pipeline for run %s finished with status %s", run_id, run.status)
                return {"status": run.status, "run_id": str(run.id), "progress_pct": run.progress_pct}
            except Exception as exc:
                logger.exception("Error executing autonomous deployment pipeline for run %s: %s", run_id, exc)
                await session.rollback()
                raise
    finally:
        await engine.dispose()
