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
async def projects_app(
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
async def client(projects_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=projects_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

async def test_create_project_endpoint(client: AsyncClient):
    payload = {
        "name": "DevOps Platform",
        "path": "/srv/projects/devops",
        "repo_url": "https://github.com/parag8487/ForgeOps",
        "settings": {"favourite": True, "embedding_backend": "voyage"}
    }
    response = await client.post("/api/v1/projects", json=payload)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["name"] == "DevOps Platform"
    assert data["settings"]["embedding_backend"] == "voyage"

async def test_create_project_invalid_settings(client: AsyncClient):
    payload = {
        "name": "Bad Project",
        "path": "/srv/projects/bad",
        "settings": {"unknown_key": 123}
    }
    response = await client.post("/api/v1/projects", json=payload)
    assert response.status_code == 400

async def test_get_project_and_activity(client: AsyncClient):
    import uuid
    pid = str(uuid.uuid4())
    res_get = await client.get(f"/api/v1/projects/{pid}")
    assert res_get.status_code == 200

    res_act = await client.get(f"/api/v1/projects/{pid}/activity")
    assert res_act.status_code == 200
    assert isinstance(res_act.json(), list)

async def test_get_project_readiness_endpoint(client: AsyncClient):
    import uuid
    pid = str(uuid.uuid4())
    res = await client.get(f"/api/v1/projects/{pid}/readiness")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["project_id"] == pid
    assert "score" in data
    assert "summary_report" in data

