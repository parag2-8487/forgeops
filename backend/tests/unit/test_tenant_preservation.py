# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests preserving project tenant isolation and verifying default tenantless service accounts."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.auth.models import User, UserRole
from src.auth.service_account import get_or_create_autonomous_service_user


@pytest.mark.asyncio
async def test_get_or_create_autonomous_service_user_none_tenant() -> None:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = None
    session.execute.return_value = mock_res

    user = await get_or_create_autonomous_service_user(session, tenant_id=None)

    assert user.tenant_id is None
    assert user.idp_subject == "service:autonomous-deploy:default"
    assert user.email == "service.autonomous-deploy@forgeops.internal"
    assert user.role == UserRole.DEVELOPER
    session.add.assert_called_once()
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_autonomous_service_user_with_tenant() -> None:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = None
    session.execute.return_value = mock_res
    t_id = uuid.uuid4()

    user = await get_or_create_autonomous_service_user(session, tenant_id=t_id)

    assert user.tenant_id == t_id
    assert user.idp_subject == f"service:autonomous-deploy:{t_id}"
    assert user.email == f"service.autonomous-deploy+{t_id}@forgeops.internal"
    assert user.role == UserRole.DEVELOPER
    session.add.assert_called_once()
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_autonomous_service_user_existing() -> None:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    existing_user = User(
        id=uuid.uuid4(),
        tenant_id=None,
        email="service.autonomous-deploy@forgeops.internal",
        name="ForgeOps Autonomous Deploy Service",
        role=UserRole.DEVELOPER,
        idp_subject="service:autonomous-deploy:default",
        is_active=True,
    )
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = existing_user
    session.execute.return_value = mock_res

    user = await get_or_create_autonomous_service_user(session, tenant_id=None)

    assert user == existing_user
    session.add.assert_not_called()
