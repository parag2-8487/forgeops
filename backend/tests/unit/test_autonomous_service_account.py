# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for tenant-scoped autonomous service account and principal."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from src.auth.models import User, UserRole
from src.auth.principal import Principal
from src.auth.service_account import get_or_create_autonomous_service_user


class MockScalars:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def first(self) -> Any | None:
        return self._items[0] if self._items else None

    def all(self) -> list[Any]:
        return list(self._items)


class MockResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> MockScalars:
        return MockScalars(self._items)


class MockDbSession:
    def __init__(self) -> None:
        self.users: list[User] = []

    def add(self, user: User) -> None:
        self.users.append(user)

    async def flush(self) -> None:
        pass

    async def execute(self, stmt: Any, params: Any = None) -> MockResult:
        matching = list(self.users)
        if hasattr(stmt, "whereclause") and stmt.whereclause is not None:
            clauses = stmt.whereclause.clauses if hasattr(stmt.whereclause, "clauses") else [stmt.whereclause]
            for c in clauses:
                if hasattr(c, "left") and hasattr(c, "right"):
                    col = getattr(c.left, "name", None)
                    val = getattr(c.right, "value", None)
                    if col and val is not None:
                        matching = [u for u in matching if getattr(u, col, None) == val]
        return MockResult(matching)


@pytest.fixture
def db_session() -> MockDbSession:
    return MockDbSession()


@pytest.mark.asyncio
async def test_service_user_requires_explicit_tenant(db_session: MockDbSession):
    with pytest.raises(ValueError, match="requires explicit tenant_id"):
        await get_or_create_autonomous_service_user(db_session, tenant_id=None)


@pytest.mark.asyncio
async def test_service_user_idempotent_creation(db_session: MockDbSession):
    tenant_id = uuid.uuid4()
    user1 = await get_or_create_autonomous_service_user(db_session, tenant_id=tenant_id)
    assert user1.tenant_id == tenant_id
    assert user1.role == UserRole.DEVELOPER
    assert user1.is_active is True
    assert f"service:autonomous-deploy:{tenant_id}" == user1.idp_subject
    assert f"service.autonomous-deploy+{tenant_id}@forgeops.internal" == user1.email

    # Idempotent second fetch returns identical record
    user2 = await get_or_create_autonomous_service_user(db_session, tenant_id=tenant_id)
    assert user2.id == user1.id


@pytest.mark.asyncio
async def test_service_user_tenant_isolation(db_session: MockDbSession):
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user_a = await get_or_create_autonomous_service_user(db_session, tenant_id=tenant_a)
    user_b = await get_or_create_autonomous_service_user(db_session, tenant_id=tenant_b)
    assert user_a.id != user_b.id
    assert user_a.tenant_id == tenant_a
    assert user_b.tenant_id == tenant_b


def test_principal_for_service_factory():
    service_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    principal = Principal.for_service(
        service_id=service_id,
        subject=f"service:autonomous-deploy:{tenant_id}",
        email=f"service.autonomous-deploy+{tenant_id}@forgeops.internal",
        tenant_id=tenant_id,
        role=UserRole.DEVELOPER,
    )
    assert principal.kind == "service"
    assert principal.user_id == service_id
    assert principal.tenant_id == tenant_id
    assert principal.role == UserRole.DEVELOPER
    assert principal.blast_radius == "workspace"
