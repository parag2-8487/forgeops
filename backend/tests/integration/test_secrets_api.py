import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from src.auth.cerbos import CerbosPrincipal
from src.auth.dependencies import require_principal
from src.main import create_app

from tests.integration.production_app import apply_committed_baseline_env
from tests.integration.wiring import wires

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory, pytest.mark.infisical]


@pytest_asyncio.fixture
async def secrets_app(
    monkeypatch: pytest.MonkeyPatch,
    schema_at_head: str,
) -> AsyncIterator[Any]:
    initial_infisical_url = os.environ.get("INFISICAL_URL")
    initial_backend = os.environ.get("SECRET_BACKEND")

    apply_committed_baseline_env(monkeypatch)

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", schema_at_head)

    if initial_backend == "infisical" or os.environ.get("SECRET_BACKEND") == "infisical":
        monkeypatch.setenv("SECRET_BACKEND", "infisical")
        target_url = initial_infisical_url or "http://localhost:8080"
        if target_url == "http://infisical:8080":
            target_url = "http://localhost:8080"
        monkeypatch.setenv("INFISICAL_URL", target_url)
        monkeypatch.setenv("INFISICAL_CLIENT_ID", os.environ.get("INFISICAL_CLIENT_ID", "ci-only"))
        monkeypatch.setenv("INFISICAL_CLIENT_SECRET", os.environ.get("INFISICAL_CLIENT_SECRET", "ci-only"))
    else:
        monkeypatch.setenv("SECRET_BACKEND", "local")
        monkeypatch.setenv("LOCAL_SECRET_SEAL_KEY", "01234567890123456789012345678901")

    app = create_app()

    def mock_principal() -> CerbosPrincipal:
        return CerbosPrincipal(id="test-user", roles=["admin"])

    app.dependency_overrides[require_principal] = mock_principal

    async with LifespanManager(app):
        yield app

    app.dependency_overrides.clear()

    engine = create_async_engine(schema_at_head)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM secrets"))
            await conn.execute(text("DELETE FROM projects"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(secrets_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=secrets_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@wires("secret_store")
async def test_secrets_crud_round_trip(secrets_app: Any, client: AsyncClient, schema_at_head: str):
    project_id = uuid.uuid4()

    engine = create_async_engine(schema_at_head)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
            {"id": project_id, "name": "Test Project", "path": "/test/project"},
        )
    await engine.dispose()

    # 1. POST
    create_resp = await client.post(
        "/api/v1/secrets",
        json={"project_id": str(project_id), "environment": "dev", "key": "DATABASE_URL", "value": "supersecret"},
    )
    assert create_resp.status_code == 200, create_resp.text
    data = create_resp.json()
    secret_id = data["id"]
    assert data["key"] == "DATABASE_URL"
    assert "value" not in data

    # 2. GET
    list_resp = await client.get(f"/api/v1/secrets?project_id={project_id}")
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert len(list_data) == 1
    assert list_data[0]["id"] == secret_id
    assert "value" not in list_data[0]

    # 3. PATCH
    patch_resp = await client.patch(f"/api/v1/secrets/{secret_id}", json={"value": "newsecret"})
    assert patch_resp.status_code == 200

    # 4. DELETE
    delete_resp = await client.delete(f"/api/v1/secrets/{secret_id}")
    assert delete_resp.status_code == 204

    # Verify deletion
    list_resp2 = await client.get(f"/api/v1/secrets?project_id={project_id}")
    assert len(list_resp2.json()) == 0
