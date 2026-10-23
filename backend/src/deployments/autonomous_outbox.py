# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous Deployment Orchestrator Transactional Outbox Publisher (Stage 7).

Reference:
- Specification: docs/superpowers/specs/2026-10-10-autonomous-deployment-orchestrator-design.md (§3.4, §5.2)
- Plan: docs/superpowers/plans/2026-10-10-autonomous-deployment-orchestrator.md (Stage 7)

Drains committed events from `autonomous_deployment_outbox` where `status = 'pending'`,
publishes each frame to Redis Pub/Sub channel `forgeops:events:autonomous-deploy:{run_id}`,
and updates row `status = 'published'`.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .autonomous_models import AutonomousDeploymentOutbox

logger = logging.getLogger(__name__)

AUTONOMOUS_EVENT_CHANNEL_PREFIX: str = "forgeops:events:autonomous-deploy:"


def get_autonomous_event_channel(run_id: uuid.UUID | str) -> str:
    """Format the canonical Redis Pub/Sub channel name for an autonomous deployment run."""
    return f"{AUTONOMOUS_EVENT_CHANNEL_PREFIX}{run_id}"


class AutonomousOutboxPublisher:
    """Publishes committed outbox events to Redis Pub/Sub and marks them published."""

    def __init__(self) -> None:
        pass

    async def publish_event(
        self,
        session: AsyncSession,
        redis: Any,
        event: AutonomousDeploymentOutbox,
    ) -> dict[str, Any]:
        """Publish a single outbox event to Redis and mark it published in the session."""
        channel = get_autonomous_event_channel(event.run_id)
        msg_payload: dict[str, Any] = {
            "id": event.id,
            "run_id": str(event.run_id),
            "event_seq": event.event_seq,
            "event_type": event.event_type,
            "payload": event.payload,
            "status": "published",
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }
        raw_msg = json.dumps(msg_payload, default=str)
        await redis.publish(channel, raw_msg)

        event.status = "published"
        session.add(event)
        return msg_payload

    async def drain_pending_events(
        self,
        session: AsyncSession,
        redis: Any,
        limit: int = 100,
    ) -> int:
        """Drains committed outbox events across all runs with status = 'pending'.

        Returns the number of events published.
        """
        stmt = (
            select(AutonomousDeploymentOutbox)
            .where(AutonomousDeploymentOutbox.status == "pending")
            .order_by(AutonomousDeploymentOutbox.id.asc())
            .limit(max(1, min(limit, 500)))
        )
        result = await session.execute(stmt)
        events = list(result.scalars().all())

        if not events:
            return 0

        for event in events:
            await self.publish_event(session, redis, event)

        await session.flush()
        return len(events)

    async def drain_run_events(
        self,
        session: AsyncSession,
        redis: Any,
        run_id: uuid.UUID | str,
        limit: int = 100,
    ) -> int:
        """Drains committed outbox events for a specific run with status = 'pending'.

        Returns the number of events published.
        """
        run_uuid = uuid.UUID(str(run_id))
        stmt = (
            select(AutonomousDeploymentOutbox)
            .where(
                AutonomousDeploymentOutbox.run_id == run_uuid,
                AutonomousDeploymentOutbox.status == "pending",
            )
            .order_by(AutonomousDeploymentOutbox.event_seq.asc())
            .limit(max(1, min(limit, 500)))
        )
        result = await session.execute(stmt)
        events = list(result.scalars().all())

        if not events:
            return 0

        for event in events:
            await self.publish_event(session, redis, event)

        await session.flush()
        return len(events)
