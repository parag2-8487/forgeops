# SPDX-License-Identifier: FSL-1.1-ALv2
"""Projects are stored and read back (design.md §6.5 revision `0009`, §11.3).

Rewritten because the handlers now open a session. Two changes to the existing cases were forced by
that and are deliberate rather than accommodations:

* The mock principal was a `CerbosPrincipal`, which has no `tenant_id`. Row visibility is
  tenant-scoped, so the stand-in has to be a real `Principal` — and a test that passed with an
  object lacking the field it scopes on was proving nothing about scoping.
* An unknown settings key returned 400 from a hand-rolled `HTTPException`; it now returns the
  registered `validation-failed` 422. Appendix C.1's problem registry is closed, so a validation
  failure goes through `RequestValidationError` rather than inventing a type at the raise site.

The create-then-read case is the one that matters most: `get_project` used to return a fixed
`"Sample Project"` for **any** id, so a round trip could not tell a stored project from the fixture.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal

from tests.integration.production_app import apply_committed_baseline_env

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

TENANT = uuid.UUID("77777777-7777-7777-7777-777777777777")
OTHER_TENANT = uuid.UUID("88888888-8888-8888-8888-888888888888")
USER = uuid.UUID("99999999-9999-9999-9999-999999999999")


def _principal(tenant_id: uuid.UUID = TENANT) -> Principal:
    return Principal.for_user(
        user_id=USER,
        subject="projects-test",
        email="owner@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=tenant_id,
    )


@pytest_asyncio.fixture
async def projects_app(monkeypatch: pytest.MonkeyPatch, schema_at_head: str) -> AsyncIterator[Any]:
    """The real app against the real migrated database.

    `apply_committed_baseline_env` deliberately points `DATABASE_URL` and `REDIS_URL` at a closed
    port, because most tests using it assert on composition and must not touch live data. These
    handlers now open a session, so this fixture overrides both AFTER the baseline — which is the
    hook that file documents for "a test that needs real data".

    `schema_at_head` brings the database to head once per session, so the `projects` table these
    tests insert into is the one revision `0009` defines rather than one the test created.
    """
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    redis_url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if redis_url:
        monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app()
    app.dependency_overrides[require_principal] = _principal
    async with LifespanManager(app):
        yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(projects_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=projects_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _payload(name: str = "DevOps Platform") -> dict[str, Any]:
    return {
        "name": name,
        "path": "/srv/projects/devops",
        "repo_url": "https://github.com/parag8487/ForgeOps",
        "settings": {"favourite": True, "embedding_backend": "voyage"},
    }


async def test_create_project_endpoint(client: AsyncClient) -> None:
    response = await client.post("/api/v1/projects", json=_payload())
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["name"] == "DevOps Platform"
    assert data["settings"]["embedding_backend"] == "voyage"
    # Database-generated, so their presence is evidence a row exists rather than that a response
    # model was constructed.
    assert data["created_at"] is not None
    assert data["updated_at"] is not None


async def test_create_project_invalid_settings(client: AsyncClient) -> None:
    body = _payload()
    body["settings"] = {"not_a_real_setting": 1}
    response = await client.post("/api/v1/projects", json=body)
    # 422 with the registered type, and the offending key named — an unknown key is refused rather
    # than dropped, because a typo in `embedding_backend` that silently kept the default would only
    # surface later as a project whose vectors are in the wrong table (D-48).
    assert response.status_code == 422, response.text
    assert "not_a_real_setting" in response.text


async def test_a_created_project_is_read_back_by_id(client: AsyncClient) -> None:
    """The defect this closes: any id used to return a fixed `Sample Project`."""
    created = (await client.post("/api/v1/projects", json=_payload("Checkout Service"))).json()

    fetched = await client.get(f"/api/v1/projects/{created['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == created["id"]
    assert fetched.json()["name"] == "Checkout Service"
    assert fetched.json()["path"] == created["path"]


async def test_an_unknown_id_is_refused_rather_than_fabricated(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/projects/{uuid.uuid4()}")
    # The non-disclosing 403, not a 404 and emphatically not a 200 carrying a fixture. §4.2 and Q-20
    # require the same body whether or not the row exists.
    assert response.status_code == 403, response.text


async def test_the_list_endpoint_returns_created_projects(client: AsyncClient) -> None:
    """`GET ""` did not exist, which is why the UI is a lookup box rather than a list."""
    first = (await client.post("/api/v1/projects", json=_payload("Alpha"))).json()
    second = (await client.post("/api/v1/projects", json=_payload("Beta"))).json()

    response = await client.get("/api/v1/projects?limit=100")
    assert response.status_code == 200, response.text
    ids = [p["id"] for p in response.json()["projects"]]
    assert first["id"] in ids
    assert second["id"] in ids


async def test_the_list_is_newest_first(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json=_payload("Older"))
    newest = (await client.post("/api/v1/projects", json=_payload("Newest"))).json()
    projects = (await client.get("/api/v1/projects?limit=100")).json()["projects"]
    assert projects[0]["id"] == newest["id"]


async def test_the_page_size_is_bounded(client: AsyncClient) -> None:
    over = await client.get("/api/v1/projects?limit=1000")
    assert over.status_code == 422, over.text


async def test_a_malformed_cursor_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/api/v1/projects?cursor=nonsense")
    assert response.status_code == 422, response.text


async def test_a_cursor_pages_without_repeating(client: AsyncClient) -> None:
    for name in ("One", "Two", "Three"):
        await client.post("/api/v1/projects", json=_payload(name))

    first_page = (await client.get("/api/v1/projects?limit=2")).json()
    assert len(first_page["projects"]) == 2
    assert first_page["next_cursor"] is not None

    second_page = (await client.get(f"/api/v1/projects?limit=2&cursor={first_page['next_cursor']}")).json()
    first_ids = {p["id"] for p in first_page["projects"]}
    second_ids = {p["id"] for p in second_page["projects"]}
    # Keyset rather than offset, so a page boundary cannot repeat a row.
    assert first_ids.isdisjoint(second_ids)


async def test_another_tenant_cannot_read_the_project(projects_app: Any, client: AsyncClient) -> None:
    created = (await client.post("/api/v1/projects", json=_payload("Private"))).json()

    projects_app.dependency_overrides[require_principal] = lambda: _principal(OTHER_TENANT)
    response = await client.get(f"/api/v1/projects/{created['id']}")
    assert response.status_code == 403, response.text

    listed = (await client.get("/api/v1/projects?limit=100")).json()["projects"]
    assert created["id"] not in {p["id"] for p in listed}


async def test_readiness_exposes_the_five_category_breakdown(client: AsyncClient) -> None:
    """§12.6 step 5 asserts a category breakdown; the response model used to drop it."""
    created = (await client.post("/api/v1/projects", json=_payload("Scored"))).json()

    response = await client.get(f"/api/v1/projects/{created['id']}/readiness")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["categories"]) == {
        "documentation_score",
        "test_coverage_score",
        "ci_config_score",
        "security_policy_score",
        "containerization_score",
    }
    assert all(isinstance(v, int) for v in body["categories"].values())
    assert 0 <= body["score"] <= 100


async def test_readiness_refuses_an_unknown_project(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/projects/{uuid.uuid4()}/readiness")
    # It used to score any id at all, which meant a readiness figure for a project that did not
    # exist.
    assert response.status_code == 403, response.text


async def test_activity_is_empty_for_a_new_project_rather_than_fabricated(client: AsyncClient) -> None:
    """It used to return one hardcoded `project_created` item dated 2026-08-06."""
    created = (await client.post("/api/v1/projects", json=_payload("Quiet"))).json()

    response = await client.get(f"/api/v1/projects/{created['id']}/activity")
    assert response.status_code == 200, response.text
    # A brand-new project has no governance transits, so an empty feed is the correct answer. The
    # feed reads `audit_events`, so it cannot disagree with the audit viewer.
    assert response.json() == []


async def test_activity_refuses_an_unknown_project(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/projects/{uuid.uuid4()}/activity")
    # Checked before the query, so an empty feed cannot be used to learn that an id is unallocated.
    assert response.status_code == 403, response.text
