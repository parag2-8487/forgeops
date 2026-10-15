# SPDX-License-Identifier: FSL-1.1-ALv2
"""Integration tests for G2 gate canonical generation and automated approval."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from src.deployments.autonomous_gates import evaluate_g2_existing_artifacts
from src.deployments.autonomous_models import AutonomousDeployment


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

    def mappings(self) -> MockScalars:
        return MockScalars(self._items)

    def scalar_one_or_none(self) -> Any | None:
        return self._items[0] if self._items else None


class MockDbSession:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}

    async def execute(self, stmt: Any, params: Any = None, **kwargs: Any) -> MockResult:
        sql = str(getattr(stmt, "text", stmt))
        if "FROM projects" in sql:
            return MockResult([{"id": uuid.uuid4(), "tenant_id": uuid.uuid4()}])
        return MockResult([])


@pytest.mark.asyncio
async def test_g2_reuses_verified_workspace_artifacts(tmp_path: Path):
    (tmp_path / "Dockerfile").write_text("FROM node:20-alpine\nEXPOSE 3000\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services:\n  app:\n    build: .\n", encoding="utf-8")

    run = AutonomousDeployment(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        strategy="docker_only",
    )
    context = {"workspace_path": str(tmp_path)}
    session = MockDbSession()

    result = await evaluate_g2_existing_artifacts(session, run, context=context)
    assert result.passed is True
    assert result.gate_id == "G2"
    assert result.details.get("action") == "REUSE"


@pytest.mark.asyncio
async def test_g2_synthesizes_via_canonical_generation(tmp_path: Path):
    project_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run_id = uuid.uuid4()
    gen_run_id = uuid.uuid4()
    change_set_id = uuid.uuid4()

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        created_by=user_id,
        strategy="docker_only",
    )

    mock_client = MagicMock(spec=httpx.AsyncClient)

    # Stream returns complete event
    async def mock_stream_chunks():
        yield f'event: progress\ndata: {{"run_id":"{gen_run_id}"}}\n\n'.encode()
        yield f'event: complete\ndata: {{"run_id":"{gen_run_id}","change_set_id":"{change_set_id}","files":["Dockerfile","docker-compose.yml"],"completion_tokens":100}}\n\n'.encode()

    class MockStreamResponse:
        status_code = 200

        def aiter_bytes(self):
            return mock_stream_chunks()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    mock_client.stream.return_value = MockStreamResponse()

    # Mock approver
    mock_chokepoint = MagicMock()
    mock_chokepoint.approve = AsyncMock()

    # Session handles change set & items query
    cs_row = {
        "id": change_set_id,
        "project_id": project_id,
        "tenant_id": tenant_id,
        "status": "pending_approval",
        "version": 1,
        "origin": "generation",
        "generation_run_id": gen_run_id,
    }
    items = [
        {"file_path": "Dockerfile", "action": "create", "new_content": "FROM node:20-alpine\n", "new_hash": "h1"},
        {
            "file_path": "docker-compose.yml",
            "action": "create",
            "new_content": "services:\n  app:\n    build: .\n",
            "new_hash": "h2",
        },
    ]

    class MockG2Session:
        def add(self, entity):
            pass

        async def flush(self):
            pass

        async def commit(self):
            pass

        async def execute(self, stmt, params=None, **kwargs):
            sql = str(getattr(stmt, "text", stmt))
            if "FROM change_sets" in sql:
                return MockResult([cs_row])
            if "FROM change_items" in sql:
                return MockResult(items)
            if "FROM projects" in sql:
                return MockResult([{"id": project_id, "tenant_id": tenant_id}])
            return MockResult([])

    session = MockG2Session()
    context = {
        "workspace_path": str(tmp_path),
        "client": mock_client,
        "chokepoint": mock_chokepoint,
        "tenant_id": tenant_id,
        "force_canonical_generation": True,
    }

    result = await evaluate_g2_existing_artifacts(session, run, context=context)
    assert result.passed is True
    assert result.gate_id == "G2"
    assert result.details.get("action") == "SYNTHESIZE"
    assert (tmp_path / "Dockerfile").exists()
    assert (tmp_path / "docker-compose.yml").exists()
    mock_chokepoint.approve.assert_awaited_once()
