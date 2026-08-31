# SPDX-License-Identifier: FSL-1.1-ALv2
"""The app fixture for tests that drive the codebase-index route (§0.4.1).

WHY THIS FILE EXISTS, AND WHY THE FIXTURES ARE NOT IN A TEST MODULE

`test_readiness_from_index.py` defined `readiness_app` and `client` itself, which was fine while it was
the only module that needed them. `test_secret_scan_surfaced.py` needs the same two — the same real app,
the same schema at head, the same device override — and importing them by NAME from a test module produces
`F811 Redefinition of unused 'client'` at every signature that takes `client`, because a fixture requested
as a parameter shadows the imported symbol.

The repository already answers this: `chokepoint_support.py` and `migration_support.py` hold fixtures for
exactly this reason, and `conftest.py` re-exports them with the note that "a test module that imported them
by NAME would shadow its own methods' parameters of the same name". This file follows that convention. The
extraction also removed a duplicated `_StubDevice`/`_device` pair that had been copy-pasted twice inside
`test_readiness_from_index.py`.

WHAT THE OVERRIDE SUBSTITUTES, AND WHAT IT MUST NOT

Only the two authentication dependencies, and only because the index route authenticates a DEVICE: an agent
holds a device token plus a client certificate and can never satisfy `require_principal`. Tests using this
fixture are about what the score and the surfacing routes do with an index, not about the credential. The
two-factor requirement and the project-scoping refusal are asserted in `test_index_route_device_auth.py`,
which is where a weaker credential is proved insufficient. Per §0.4.1 this substitutes a transport and an
identity, never a collaborator: the app, the database, Redis and every service are real.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from src.auth.dependencies import require_principal
from src.auth.device_dependencies import require_device
from src.auth.principal import Principal, UserRole

from .production_app import apply_committed_baseline_env

#: Fixed so two runs of one test see the same tenant and user. Random ids would make a failure that
#: depends on ordering impossible to reproduce.
TENANT = uuid.UUID("11112222-3333-4444-5555-666677778888")
USER = uuid.UUID("99990000-1111-2222-3333-444455556666")


class _StubDevice:
    """What `authenticate_session` returns, reduced to the two fields the index route reads."""

    def __init__(self, project_id: uuid.UUID) -> None:
        self.project_id = project_id
        self.tenant_id = None


def _device(request: Request) -> _StubDevice:
    """A device paired to WHICHEVER project the request names.

    Adopts the requested project rather than pinning one, because these tests create their projects
    dynamically. That makes the route's project-scoping check a no-op here, which is deliberate: it is
    asserted in `test_index_route_device_auth.py` together with the two-factor refusals.
    """
    return _StubDevice(uuid.UUID(str(request.path_params["project_id"])))


def _principal() -> Principal:
    return Principal.for_user(
        user_id=USER,
        subject="readiness-test",
        email="scorer@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=TENANT,
    )


@pytest_asyncio.fixture
async def readiness_app(monkeypatch: pytest.MonkeyPatch, schema_at_head: str) -> AsyncIterator[Any]:
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    redis_url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if redis_url:
        monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app()
    app.dependency_overrides[require_principal] = _principal
    app.dependency_overrides[require_device] = _device
    async with LifespanManager(app):
        yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(readiness_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=readiness_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def project_id(client: AsyncClient) -> AsyncIterator[str]:
    """One project owned by the caller's tenant, created through the real route.

    Created rather than inserted, so a test that reads it back is reading a row the API wrote.
    """
    response = await client.post(
        "/api/v1/projects",
        json={
            "name": "indexed-subject",
            "path": "/srv/projects/indexed",
            "repo_url": "https://github.com/parag8487/ForgeOps",
            "settings": {},
        },
    )
    assert response.status_code == 201, response.text
    yield str(response.json()["id"])
