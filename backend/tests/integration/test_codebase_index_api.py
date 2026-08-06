# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
import pytest_asyncio
from typing import AsyncIterator, Any
from httpx import AsyncClient, ASGITransport

from src.auth.dependencies import require_principal
from src.auth.cerbos import CerbosPrincipal
from asgi_lifespan import LifespanManager
from tests.integration.production_app import apply_committed_baseline_env

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

@pytest_asyncio.fixture
async def analysis_app(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Any]:
    from src.main import create_app
    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")

    app = create_app()

    def mock_principal() -> CerbosPrincipal:
        return CerbosPrincipal(id="test-user", roles=["admin"])
    app.dependency_overrides[require_principal] = mock_principal

    async with LifespanManager(app):
        yield app

    app.dependency_overrides.clear()

@pytest_asyncio.fixture
async def client(analysis_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=analysis_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

async def test_codebase_status_endpoint(client: AsyncClient):
    response = await client.get("/api/v1/analysis/codebase/status")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "ready"
    assert "indexed_files" in data
    assert "languages" in data

async def test_query_symbols_endpoint(client: AsyncClient):
    response = await client.get("/api/v1/analysis/codebase/symbols?query=NewParser")
    assert response.status_code == 200, response.text
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["name"] == "NewParser"

async def test_get_chunk_details_endpoint(client: AsyncClient):
    response = await client.get("/api/v1/analysis/codebase/chunks/chunk-123")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["chunk_id"] == "chunk-123"
    assert "file_path" in data
    assert "content" in data
