# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for scoped automated governance approval and artifact materialization."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.auth.models import UserRole
from src.deployments.autonomous_governance import (
    ApprovalScopeError,
    MaterializationError,
    approve_and_materialize_generation_change_set,
)


class MockMappingResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class MockQueryResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> MockMappingResult:
        return MockMappingResult(self._rows)

    def scalars(self):
        class _Scalars:
            def __init__(self, r):
                self._r = r
            def first(self):
                return self._r[0] if self._r else None
            def all(self):
                return list(self._r)
        return _Scalars(self._rows)


class MockGovernanceSession:
    def __init__(
        self,
        change_set: dict[str, Any] | None,
        items: list[dict[str, Any]] | None = None,
    ) -> None:
        self.change_set = change_set
        self.items = items or []
        self.added = []

    def add(self, entity: Any) -> None:
        self.added.append(entity)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def execute(self, stmt: Any, params: Any = None, **kwargs: Any) -> MockQueryResult:
        sql = str(getattr(stmt, "text", stmt))
        if "FROM change_sets" in sql:
            if self.change_set is not None:
                return MockQueryResult([self.change_set])
            return MockQueryResult([])
        if "FROM change_items" in sql:
            return MockQueryResult(self.items)
        if "FROM users" in sql:
            # For get_or_create_autonomous_service_user
            return MockQueryResult([])
        return MockQueryResult([])


@pytest.mark.asyncio
async def test_approval_rejects_missing_change_set(tmp_path: Path):
    session = MockGovernanceSession(change_set=None)
    with pytest.raises(ApprovalScopeError, match="not found"):
        await approve_and_materialize_generation_change_set(
            session,
            chokepoint=MagicMock(),
            change_set_id=uuid.uuid4(),
            generation_run_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            initiating_user_id=uuid.uuid4(),
            workspace_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_approval_rejects_unmatched_project(tmp_path: Path):
    cs_id = uuid.uuid4()
    gen_id = uuid.uuid4()
    proj_a = uuid.uuid4()
    proj_b = uuid.uuid4()
    tenant = uuid.uuid4()
    cs = {
        "id": cs_id,
        "project_id": proj_a,
        "tenant_id": tenant,
        "status": "pending_approval",
        "version": 1,
        "origin": "generation",
        "generation_run_id": gen_id,
    }
    session = MockGovernanceSession(change_set=cs)
    with pytest.raises(ApprovalScopeError, match="does not match run project"):
        await approve_and_materialize_generation_change_set(
            session,
            chokepoint=MagicMock(),
            change_set_id=cs_id,
            generation_run_id=gen_id,
            run_id=uuid.uuid4(),
            project_id=proj_b,
            tenant_id=tenant,
            initiating_user_id=uuid.uuid4(),
            workspace_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_approval_rejects_unmatched_tenant(tmp_path: Path):
    cs_id = uuid.uuid4()
    gen_id = uuid.uuid4()
    proj = uuid.uuid4()
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    cs = {
        "id": cs_id,
        "project_id": proj,
        "tenant_id": tenant_a,
        "status": "pending_approval",
        "version": 1,
        "origin": "generation",
        "generation_run_id": gen_id,
    }
    session = MockGovernanceSession(change_set=cs)
    with pytest.raises(ApprovalScopeError, match="does not match run tenant"):
        await approve_and_materialize_generation_change_set(
            session,
            chokepoint=MagicMock(),
            change_set_id=cs_id,
            generation_run_id=gen_id,
            run_id=uuid.uuid4(),
            project_id=proj,
            tenant_id=tenant_b,
            initiating_user_id=uuid.uuid4(),
            workspace_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_approval_rejects_unmatched_generation_run(tmp_path: Path):
    cs_id = uuid.uuid4()
    gen_a = uuid.uuid4()
    gen_b = uuid.uuid4()
    proj = uuid.uuid4()
    tenant = uuid.uuid4()
    cs = {
        "id": cs_id,
        "project_id": proj,
        "tenant_id": tenant,
        "status": "pending_approval",
        "version": 1,
        "origin": "generation",
        "generation_run_id": gen_a,
    }
    session = MockGovernanceSession(change_set=cs)
    with pytest.raises(ApprovalScopeError, match="does not match current run"):
        await approve_and_materialize_generation_change_set(
            session,
            chokepoint=MagicMock(),
            change_set_id=cs_id,
            generation_run_id=gen_b,
            run_id=uuid.uuid4(),
            project_id=proj,
            tenant_id=tenant,
            initiating_user_id=uuid.uuid4(),
            workspace_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_approval_rejects_non_generation_origin(tmp_path: Path):
    cs_id = uuid.uuid4()
    gen_id = uuid.uuid4()
    proj = uuid.uuid4()
    tenant = uuid.uuid4()
    cs = {
        "id": cs_id,
        "project_id": proj,
        "tenant_id": tenant,
        "status": "pending_approval",
        "version": 1,
        "origin": "manual",
        "generation_run_id": gen_id,
    }
    session = MockGovernanceSession(change_set=cs)
    with pytest.raises(ApprovalScopeError, match="is not 'generation'"):
        await approve_and_materialize_generation_change_set(
            session,
            chokepoint=MagicMock(),
            change_set_id=cs_id,
            generation_run_id=gen_id,
            run_id=uuid.uuid4(),
            project_id=proj,
            tenant_id=tenant,
            initiating_user_id=uuid.uuid4(),
            workspace_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_approval_rejects_k8s_artifacts_in_docker_only(tmp_path: Path):
    cs_id = uuid.uuid4()
    gen_id = uuid.uuid4()
    proj = uuid.uuid4()
    tenant = uuid.uuid4()
    cs = {
        "id": cs_id,
        "project_id": proj,
        "tenant_id": tenant,
        "status": "pending_approval",
        "version": 1,
        "origin": "generation",
        "generation_run_id": gen_id,
    }
    items = [
        {"file_path": "k8s/deployment.yaml", "action": "create", "new_content": "apiVersion: apps/v1", "new_hash": "abc"}
    ]
    session = MockGovernanceSession(change_set=cs, items=items)
    with pytest.raises(ApprovalScopeError, match="forbids Kubernetes/IaC"):
        await approve_and_materialize_generation_change_set(
            session,
            chokepoint=MagicMock(),
            change_set_id=cs_id,
            generation_run_id=gen_id,
            run_id=uuid.uuid4(),
            project_id=proj,
            tenant_id=tenant,
            initiating_user_id=uuid.uuid4(),
            workspace_dir=tmp_path,
            strategy="docker_only",
        )


@pytest.mark.asyncio
async def test_materialization_path_traversal_refused(tmp_path: Path):
    cs_id = uuid.uuid4()
    gen_id = uuid.uuid4()
    proj = uuid.uuid4()
    tenant = uuid.uuid4()
    cs = {
        "id": cs_id,
        "project_id": proj,
        "tenant_id": tenant,
        "status": "pending_approval",
        "version": 1,
        "origin": "generation",
        "generation_run_id": gen_id,
    }
    items = [
        {"file_path": "../../etc/passwd", "action": "create", "new_content": "bad", "new_hash": "abc"}
    ]
    session = MockGovernanceSession(change_set=cs, items=items)
    chokepoint = MagicMock()
    chokepoint.approve = AsyncMock()

    with pytest.raises(MaterializationError, match="Path traversal attempt"):
        await approve_and_materialize_generation_change_set(
            session,
            chokepoint=chokepoint,
            change_set_id=cs_id,
            generation_run_id=gen_id,
            run_id=uuid.uuid4(),
            project_id=proj,
            tenant_id=tenant,
            initiating_user_id=uuid.uuid4(),
            workspace_dir=tmp_path,
            strategy="docker_only",
        )


@pytest.mark.asyncio
async def test_successful_approval_and_materialization(tmp_path: Path):
    cs_id = uuid.uuid4()
    gen_id = uuid.uuid4()
    proj = uuid.uuid4()
    tenant = uuid.uuid4()
    init_user = uuid.uuid4()
    run_id = uuid.uuid4()

    cs = {
        "id": cs_id,
        "project_id": proj,
        "tenant_id": tenant,
        "status": "pending_approval",
        "version": 2,
        "origin": "generation",
        "generation_run_id": gen_id,
    }
    items = [
        {"file_path": "Dockerfile", "action": "create", "new_content": "FROM node:20-alpine\n", "new_hash": "h1"},
        {"file_path": "docker-compose.yml", "action": "create", "new_content": "services:\n  app:\n    build: .\n", "new_hash": "h2"},
    ]
    session = MockGovernanceSession(change_set=cs, items=items)
    chokepoint = MagicMock()
    chokepoint.approve = AsyncMock()

    await approve_and_materialize_generation_change_set(
        session,
        chokepoint=chokepoint,
        change_set_id=cs_id,
        generation_run_id=gen_id,
        run_id=run_id,
        project_id=proj,
        tenant_id=tenant,
        initiating_user_id=init_user,
        workspace_dir=tmp_path,
        strategy="docker_only",
    )

    # Verify files created in workspace
    assert (tmp_path / "Dockerfile").read_text(encoding="utf-8") == "FROM node:20-alpine\n"
    assert (tmp_path / "docker-compose.yml").read_text(encoding="utf-8") == "services:\n  app:\n    build: .\n"
    assert (tmp_path / ".forgeops" / "provenance.json").exists()

    # Verify chokepoint.approve was called with correct parameters
    chokepoint.approve.assert_awaited_once()
    _, kwargs = chokepoint.approve.await_args
    assert kwargs["change_set_id"] == cs_id
    assert kwargs["expected_version"] == 2
    assert kwargs["principal"].kind == "service"
    assert kwargs["principal"].tenant_id == tenant
    parsed_comment = json.loads(kwargs["comment"])
    assert parsed_comment["autonomous_run_id"] == str(run_id)
    assert parsed_comment["initiating_user_id"] == str(init_user)
    assert parsed_comment["strategy"] == "docker_only"


@pytest.mark.asyncio
async def test_device_not_connected_delivery_refusal_succeeds_materialization(tmp_path: Path):
    from src.core.errors import problem

    cs_id = uuid.uuid4()
    gen_id = uuid.uuid4()
    proj = uuid.uuid4()
    tenant = uuid.uuid4()
    init_user = uuid.uuid4()
    run_id = uuid.uuid4()

    cs = {
        "id": cs_id,
        "project_id": proj,
        "tenant_id": tenant,
        "status": "pending_approval",
        "version": 1,
        "origin": "generation",
        "generation_run_id": gen_id,
    }
    items = [
        {"file_path": "Dockerfile", "action": "create", "new_content": "FROM node:20-alpine\n", "new_hash": "h1"},
    ]
    session = MockGovernanceSession(change_set=cs, items=items)
    chokepoint = MagicMock()
    chokepoint.approve = AsyncMock(
        side_effect=problem(
            "device-not-connected",
            detail="device has no live agent session",
        )
    )

    await approve_and_materialize_generation_change_set(
        session,
        chokepoint=chokepoint,
        change_set_id=cs_id,
        generation_run_id=gen_id,
        run_id=run_id,
        project_id=proj,
        tenant_id=tenant,
        initiating_user_id=init_user,
        workspace_dir=tmp_path,
        strategy="docker_only",
    )

    # Materialization must still complete successfully
    assert (tmp_path / "Dockerfile").read_text(encoding="utf-8") == "FROM node:20-alpine\n"
    assert (tmp_path / ".forgeops" / "provenance.json").exists()
