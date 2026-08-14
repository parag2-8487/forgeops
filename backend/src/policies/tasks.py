# SPDX-License-Identifier: FSL-1.1-ALv2
"""Policy bundle distribution task handler (design.md §7.9, §11.7)."""

from __future__ import annotations

import uuid
from typing import Any

from src.core.config import get_settings
from src.core.db import create_db_engine, create_sessionmaker
from src.core.tasks import register_task

from .models import PolicyBundle

_sessionmaker = None


async def _init_globals() -> None:
    global _sessionmaker
    if _sessionmaker is not None:
        return
    settings = get_settings()
    engine = create_db_engine(settings)
    _sessionmaker = create_sessionmaker(engine)


@register_task("policy.bundle.publish")
async def publish_bundle_task(payload: dict[str, Any]) -> None:
    """Deliver the bundle notification to connected agent devices."""
    await _init_globals()
    assert _sessionmaker is not None

    bundle_id = uuid.UUID(payload["bundle_id"])

    async with _sessionmaker() as session:
        bundle = await session.get(PolicyBundle, bundle_id)
        if not bundle:
            return
