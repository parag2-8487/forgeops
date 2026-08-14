# SPDX-License-Identifier: FSL-1.1-ALv2
"""Policy bundle distribution task handler (design.md §7.9, §11.7)."""

from __future__ import annotations

import base64
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlmodel import select
from src.auth.device_models import AgentDevice, DeviceStatus
from src.core.config import get_settings
from src.core.db import create_db_engine, create_sessionmaker
from src.core.tasks import register_task
from src.policies.models import PolicyBundle
from src.websocket.hub import AgentHub

_sessionmaker = None
_hub = None


async def _init_globals() -> None:
    global _sessionmaker, _hub
    if _sessionmaker is not None:
        return
    settings = get_settings()
    engine = create_db_engine(settings)
    _sessionmaker = create_sessionmaker(engine)
    redis_url = str(settings.redis_url)
    redis = Redis.from_url(redis_url)
    _hub = AgentHub(redis)


@register_task("policy.bundle.publish")
async def publish_bundle_task(payload: dict[str, Any]) -> None:
    """Deliver the bundle notification to connected agent devices."""
    await _init_globals()
    assert _sessionmaker is not None
    assert _hub is not None

    bundle_id = uuid.UUID(payload["bundle_id"])
    project_id_str = payload.get("project_id")
    project_id = uuid.UUID(project_id_str) if project_id_str else None

    async with _sessionmaker() as session:
        bundle = await session.get(PolicyBundle, bundle_id)
        if not bundle:
            return

        stmt = select(AgentDevice).where(AgentDevice.status == DeviceStatus.ACTIVE)
        if project_id:
            stmt = stmt.where(AgentDevice.project_id == project_id)

        result = await session.execute(stmt)
        devices = result.scalars().all()

        payload_b64 = base64.b64encode(bundle.bundle).decode("utf-8")

        for device in devices:
            try:
                await _hub.send_command(
                    device_id=device.id,
                    command={
                        "type": "policy.bundle.published",
                        "bundle_id": str(bundle.id),
                        "digest": bundle.digest,
                        "bundle": payload_b64,
                    },
                )
            except Exception:
                pass
