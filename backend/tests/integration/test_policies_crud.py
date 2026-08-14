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

from tests.integration.production_app import apply_committed_baseline_env


@pytest_asyncio.fixture
async def policies_app(
    monkeypatch: pytest.MonkeyPatch,
    schema_at_head: str,
) -> AsyncIterator[Any]:
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", schema_at_head)

    app = create_app()

    def mock_principal() -> CerbosPrincipal:
        return CerbosPrincipal(id="test-user", roles=["admin"])

    app.dependency_overrides[require_principal] = mock_principal

    async with LifespanManager(app):
        yield app

    app.dependency_overrides.clear()

    # Cleanup test policies
    engine = create_async_engine(schema_at_head)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM policies WHERE name = 'Test Policy' OR name = 'Updated Policy'"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(policies_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=policies_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.mark.asyncio
async def test_policy_crud_lifecycle(
    client: AsyncClient,
    preset_env: dict[str, str] = None,
) -> None:
    # 1. Create a policy
    create_payload = {
        "name": "Test Policy",
        "rego_rules": "package forgeops\n\ndefault allow = true\n",
        "enabled": True,
        "engine": "rego",
    }
    resp = await client.post("/api/v1/policies", json=create_payload)
    assert resp.status_code == 201, resp.text
    policy_data = resp.json()
    assert policy_data["name"] == "Test Policy"
    assert policy_data["enabled"] is True
    policy_id = policy_data["id"]

    # 2. Get the policy
    resp = await client.get(f"/api/v1/policies/{policy_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == policy_id

    # 3. Update the policy
    patch_payload = {"enabled": False}
    resp = await client.patch(f"/api/v1/policies/{policy_id}", json=patch_payload)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    # 4. Delete the policy
    resp = await client.delete(f"/api/v1/policies/{policy_id}")
    assert resp.status_code == 204

    # 5. Ensure it's deleted
    resp = await client.get(f"/api/v1/policies/{policy_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_policy_templates(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/policies/templates")
    assert resp.status_code == 200
    templates = resp.json()
    assert isinstance(templates, list)
    assert any(t["id"] == "scheduling" for t in templates)
    assert any(t["id"] == "file_restrictions" for t in templates)


@pytest.mark.asyncio
async def test_policy_create_invalid_rego(client: AsyncClient) -> None:
    create_payload = {
        "name": "Invalid Policy",
        "rego_rules": "package forgeops\ndefault allow == true",  # syntax error
        "enabled": True,
    }
    resp = await client.post("/api/v1/policies", json=create_payload)
    assert resp.status_code == 422
    assert "rego_parse_error" in resp.text


@pytest.mark.asyncio
async def test_policy_dry_run(client: AsyncClient) -> None:
    # 1. Create a policy
    create_payload = {
        "name": "Dry Run Policy",
        "rego_rules": (
            "package forgeops.governance\n\n"
            'default decision = "deny"\n\n'
            'decision = "allow" if { input.action == "allow_me" }\n'
        ),
    }
    resp = await client.post("/api/v1/policies", json=create_payload)
    assert resp.status_code == 201
    policy_id = resp.json()["id"]

    # 2. Dry run with allowed input
    resp = await client.post(f"/api/v1/policies/{policy_id}/test", json={"input": {"action": "allow_me"}})
    assert resp.status_code == 200
    assert resp.json()["decision"] == "allow"

    # 3. Dry run with denied input
    resp = await client.post(f"/api/v1/policies/{policy_id}/test", json={"input": {"action": "deny_me"}})
    assert resp.status_code == 200
    assert resp.json()["decision"] == "deny"
