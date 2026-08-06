import pytest
import pytest_asyncio
import uuid
from typing import AsyncIterator, Any
from httpx import AsyncClient, ASGITransport

from src.auth.dependencies import require_principal
from src.auth.cerbos import CerbosPrincipal
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from asgi_lifespan import LifespanManager
from tests.integration.production_app import apply_committed_baseline_env

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory, pytest.mark.infisical]

@pytest_asyncio.fixture
async def secrets_app(
    monkeypatch: pytest.MonkeyPatch,
    schema_at_head: str,
) -> AsyncIterator[Any]:
    from src.main import create_app
    apply_committed_baseline_env(monkeypatch)
    import os
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    
    if os.environ.get("SECRET_BACKEND") == "infisical":
        monkeypatch.setenv("SECRET_BACKEND", "infisical")
        # Ensure Infisical env vars exist
        monkeypatch.setenv("INFISICAL_URL", os.environ.get("INFISICAL_URL", "http://localhost:8080"))
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

async def test_secrets_crud_round_trip(secrets_app: Any, client: AsyncClient, schema_at_head: str):
    project_id = uuid.uuid4()
    
    engine = create_async_engine(schema_at_head)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO projects (id, name) VALUES (:id, :name)"),
            {"id": project_id, "name": "Test Project"}
        )
    await engine.dispose()
    
    # 1. POST
    create_resp = await client.post("/api/v1/secrets", json={
        "project_id": str(project_id),
        "environment": "dev",
        "key": "DATABASE_URL",
        "value": "supersecret"
    })
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
    patch_resp = await client.patch(f"/api/v1/secrets/{secret_id}", json={
        "value": "newsecret"
    })
    assert patch_resp.status_code == 200
    
    # 4. DELETE
    delete_resp = await client.delete(f"/api/v1/secrets/{secret_id}")
    assert delete_resp.status_code == 204
    
    # Verify deletion
    list_resp2 = await client.get(f"/api/v1/secrets?project_id={project_id}")
    assert len(list_resp2.json()) == 0
