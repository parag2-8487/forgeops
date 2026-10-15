# SPDX-License-Identifier: FSL-1.1-ALv2
"""Service account provisioning and lifecycle management."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import User, UserRole


async def get_or_create_autonomous_service_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
) -> User:
    """Idempotently resolves or provisions a dedicated service-account User for Autonomous Deploy.

    Rejects missing tenant context: an automated deployment service actor must always be strictly
    scoped to the target project's tenant boundary.
    """
    if tenant_id is None:
        raise ValueError("Autonomous service account requires explicit tenant_id context.")

    idp_subject = f"service:autonomous-deploy:{tenant_id}"
    email = f"service.autonomous-deploy+{tenant_id}@forgeops.internal"

    stmt = select(User).where(User.tenant_id == tenant_id, User.idp_subject == idp_subject)
    res = await session.execute(stmt)
    existing = res.scalars().first()
    if existing is not None:
        return existing

    # Create dedicated tenant-scoped service account
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        email=email,
        name="ForgeOps Autonomous Deploy Service",
        role=UserRole.DEVELOPER,
        idp_subject=idp_subject,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user
