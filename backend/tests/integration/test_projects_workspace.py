# SPDX-License-Identifier: FSL-1.1-ALv2
"""Search, tags, favourites, archive and delete — PRD FR-02, FR-03 and FR-05.

All three were P0 and none had a route on either side, so this module is new rather than a rewrite.
Four properties are worth naming because each is a decision a reviewer should be able to challenge:

**The filters are asserted to run in SQL, not in the response.** A test that fetched every project
and checked the browser-side result would pass over an implementation that pages the unfiltered
sequence — and that implementation is wrong in a way that only appears on page two of a search. So
`test_search_pages_the_filtered_set` asks for a page size smaller than the match count and asserts
the cursor walks matches only.

**Favourites are asserted to be per user.** `test_a_favourite_belongs_to_the_person_who_made_it`
switches the principal and asserts the flag flips back to false, which is the whole reason
`projects.settings.favourite` was the wrong shape and needed a table.

**Delete is asserted to preserve audit rows.** Revision `0007` gave `audit_events.project_id` no
foreign key on purpose. That intent is only real if something checks it, and nothing did.

**The tag filter is asserted CONJUNCTIVE.** `?tag=a&tag=b` must mean both. A disjunctive
implementation passes any test that only ever sends one tag.
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal

from tests.integration.production_app import apply_committed_baseline_env

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")
#: Two users in one tenant, which is the situation `projects.settings.favourite` could not model.
USER = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
OTHER_USER = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")


def _principal(user_id: uuid.UUID = USER) -> Principal:
    return Principal.for_user(
        user_id=user_id,
        subject=f"workspace-test-{user_id}",
        email=f"{user_id}@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=TENANT,
    )


@pytest_asyncio.fixture
async def workspace_app(monkeypatch: pytest.MonkeyPatch, schema_at_head: str) -> AsyncIterator[Any]:
    """The real app, plus the two `users` rows favourites need.

    `project_favourites.user_id` is a foreign key into `users`, deliberately: a favourite pointing at
    a user who does not exist is not a record of anything. So the fixture inserts the two principals
    rather than the test discovering the constraint as an opaque 500 — which is also a small proof
    that the FK is really there.
    """
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    redis_url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if redis_url:
        monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("APP_ENV", "test")

    engine = create_async_engine(schema_at_head)
    try:
        async with engine.begin() as conn:
            for user_id in (USER, OTHER_USER):
                await conn.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, email, name, role, idp_subject, is_active) "
                        "VALUES (:id, :tenant, :email, :name, 'admin', :subject, true) "
                        "ON CONFLICT (idp_subject) DO NOTHING"
                    ),
                    {
                        "id": user_id,
                        "tenant": TENANT,
                        "email": f"{user_id}@example.invalid",
                        "name": "Workspace Test",
                        "subject": f"workspace-test-{user_id}",
                    },
                )
    finally:
        await engine.dispose()

    app = create_app()
    app.dependency_overrides[require_principal] = _principal
    async with LifespanManager(app):
        yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(workspace_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=workspace_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _create(client: AsyncClient, name: str, path: str = "/srv/x") -> dict[str, Any]:
    response = await client.post("/api/v1/projects", json={"name": name, "path": path, "settings": {}})
    assert response.status_code == 201, response.text
    return response.json()


# ─── the shape of a project row ──────────────────────────────────────────────────────────────────


async def test_a_new_project_reports_no_tags_no_favourite_and_no_indexed_files(client: AsyncClient) -> None:
    """The three fields the list screen needs, and the one that replaces `readinessScore: 0`.

    `indexed_file_count` is zero because nothing has scanned it, which is the fact the projects screen
    used to render as a hardcoded readiness score of 0 for every project regardless of its real one.
    Zero files means "never scanned", and the UI says that in words rather than showing a number.
    """
    created = await _create(client, "Fresh")
    assert created["tags"] == []
    assert created["favourite"] is False
    assert created["indexed_file_count"] == 0
    assert created["archived_at"] is None


# ─── FR-02: tags ─────────────────────────────────────────────────────────────────────────────────


async def test_a_tag_is_added_lower_cased_and_adding_it_twice_is_idempotent(client: AsyncClient) -> None:
    project = await _create(client, "Tagged")

    first = await client.put(f"/api/v1/projects/{project['id']}/tags", json={"tag": "Production"})
    assert first.status_code == 200, first.text
    # Lower-cased: `Prod` and `prod` being two tags would split the filter without anyone intending
    # it, and the list screen would show both.
    assert first.json()["tags"] == ["production"]

    again = await client.put(f"/api/v1/projects/{project['id']}/tags", json={"tag": "production"})
    assert again.status_code == 200
    assert again.json()["tags"] == ["production"]


async def test_a_tag_is_removed_and_removing_an_absent_tag_succeeds(client: AsyncClient) -> None:
    project = await _create(client, "Tagged")
    await client.put(f"/api/v1/projects/{project['id']}/tags", json={"tag": "eu"})

    removed = await client.delete(f"/api/v1/projects/{project['id']}/tags/eu")
    assert removed.status_code == 200, removed.text
    assert removed.json()["tags"] == []

    # Idempotent. A 404 here would make a UI that removes a tag twice show an error for a state that
    # is exactly what the user asked for.
    again = await client.delete(f"/api/v1/projects/{project['id']}/tags/eu")
    assert again.status_code == 200
    assert again.json()["tags"] == []


async def test_the_tag_filter_requires_every_tag_not_any(client: AsyncClient) -> None:
    """Conjunctive, which a single-tag test cannot distinguish from disjunctive."""
    both = await _create(client, "Both tags")
    one = await _create(client, "One tag")
    for tag in ("prod", "eu"):
        await client.put(f"/api/v1/projects/{both['id']}/tags", json={"tag": tag})
    await client.put(f"/api/v1/projects/{one['id']}/tags", json={"tag": "prod"})

    matched = (await client.get("/api/v1/projects?tag=prod&tag=eu&limit=100")).json()["projects"]
    ids = {p["id"] for p in matched}
    assert both["id"] in ids
    assert one["id"] not in ids, "the tag filter widened as terms were added, so it is disjunctive"


async def test_the_tenant_tag_vocabulary_is_readable(client: AsyncClient) -> None:
    """So the filter can be a chooser rather than a free-text box that silently matches nothing."""
    project = await _create(client, "Vocab")
    await client.put(f"/api/v1/projects/{project['id']}/tags", json={"tag": "zeta"})

    tags = (await client.get("/api/v1/projects/tags")).json()
    assert "zeta" in tags
    # Declared before `/{project_id}`, so this is not swallowed by the parameterised route and
    # answered as "zeta is not a valid uuid".
    assert isinstance(tags, list)


# ─── FR-03: favourites ───────────────────────────────────────────────────────────────────────────


async def test_a_favourite_belongs_to_the_person_who_made_it(workspace_app: Any, client: AsyncClient) -> None:
    """The reason this needed a table rather than the `settings.favourite` flag from revision 0009.

    That flag is per PROJECT, so one person starring a project would reorder the other's list. Here
    the same project is read by a second principal in the SAME tenant and is not their favourite.
    """
    project = await _create(client, "Starred")

    starred = await client.put(f"/api/v1/projects/{project['id']}/favourite")
    assert starred.status_code == 200, starred.text
    assert starred.json()["favourite"] is True

    workspace_app.dependency_overrides[require_principal] = lambda: _principal(OTHER_USER)
    as_other = await client.get(f"/api/v1/projects/{project['id']}")
    assert as_other.status_code == 200
    assert as_other.json()["favourite"] is False, "a favourite leaked from one user to another"


async def test_the_favourite_filter_selects_and_excludes(client: AsyncClient) -> None:
    starred = await _create(client, "Starred")
    plain = await _create(client, "Plain")
    await client.put(f"/api/v1/projects/{starred['id']}/favourite")

    only = {p["id"] for p in (await client.get("/api/v1/projects?favourite=true&limit=100")).json()["projects"]}
    assert starred["id"] in only
    assert plain["id"] not in only

    without = {p["id"] for p in (await client.get("/api/v1/projects?favourite=false&limit=100")).json()["projects"]}
    assert plain["id"] in without
    assert starred["id"] not in without


async def test_unstarring_is_idempotent(client: AsyncClient) -> None:
    project = await _create(client, "Starred")
    await client.put(f"/api/v1/projects/{project['id']}/favourite")

    first = await client.delete(f"/api/v1/projects/{project['id']}/favourite")
    assert first.status_code == 200
    assert first.json()["favourite"] is False
    again = await client.delete(f"/api/v1/projects/{project['id']}/favourite")
    assert again.status_code == 200


# ─── FR-02: search ───────────────────────────────────────────────────────────────────────────────


async def test_search_matches_the_name_or_the_path_case_insensitively(client: AsyncClient) -> None:
    by_name = await _create(client, "Checkout Service", path="/srv/unrelated")
    by_path = await _create(client, "Unrelated", path="/srv/checkout/api")
    neither = await _create(client, "Billing", path="/srv/billing")

    found = {p["id"] for p in (await client.get("/api/v1/projects?search=CHECKOUT&limit=100")).json()["projects"]}
    assert by_name["id"] in found
    assert by_path["id"] in found
    assert neither["id"] not in found


async def test_a_percent_in_the_search_term_is_a_literal_character(client: AsyncClient) -> None:
    """A `%` typed into a search box is the character the operator typed, not a wildcard.

    Under `LIKE '%' || :q || '%'` a bare `%` matches every row, so a user searching for a literal
    percent sign would be shown their whole workspace and told it matched.
    """
    literal = await _create(client, "Discount 50% off")
    other = await _create(client, "Billing")

    found = {p["id"] for p in (await client.get("/api/v1/projects?search=50%25&limit=100")).json()["projects"]}
    assert literal["id"] in found
    assert other["id"] not in found


async def test_search_pages_the_filtered_set(client: AsyncClient) -> None:
    """The property a browser-side filter cannot have.

    Three matches, a page size of two: if the filter ran after paging, `next_cursor` would describe
    the unfiltered sequence and the second page would contain non-matches or drop matches entirely.
    """
    wanted = [await _create(client, f"Match {n}", path="/srv/match") for n in range(3)]
    for n in range(3):
        await _create(client, f"Other {n}", path="/srv/other")

    first = (await client.get("/api/v1/projects?search=match&limit=2")).json()
    assert len(first["projects"]) == 2
    assert first["next_cursor"] is not None

    second = (await client.get(f"/api/v1/projects?search=match&limit=2&cursor={first['next_cursor']}")).json()
    seen = [p["id"] for p in first["projects"]] + [p["id"] for p in second["projects"]]
    assert sorted(seen) == sorted(p["id"] for p in wanted)
    assert second["next_cursor"] is None


# ─── FR-05: archive ──────────────────────────────────────────────────────────────────────────────


async def test_archiving_hides_a_project_from_the_default_list_and_is_reversible(client: AsyncClient) -> None:
    project = await _create(client, "Finished")

    archived = await client.post(f"/api/v1/projects/{project['id']}/archive", json={"reason": "work concluded"})
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None

    default_list = {p["id"] for p in (await client.get("/api/v1/projects?limit=100")).json()["projects"]}
    assert project["id"] not in default_list

    # A separate view rather than an inclusive flag: asking for archived shows those and only those.
    archived_list = (await client.get("/api/v1/projects?archived=true&limit=100")).json()["projects"]
    assert project["id"] in {p["id"] for p in archived_list}
    assert all(p["archived_at"] is not None for p in archived_list)

    restored = await client.post(f"/api/v1/projects/{project['id']}/unarchive", json={"reason": "resumed"})
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None
    assert project["id"] in {p["id"] for p in (await client.get("/api/v1/projects?limit=100")).json()["projects"]}


async def test_archiving_twice_keeps_the_first_timestamp(client: AsyncClient) -> None:
    """ "When did this stop being worked on" is the first time, not the most recent click."""
    project = await _create(client, "Finished")
    first = (await client.post(f"/api/v1/projects/{project['id']}/archive", json={"reason": "done"})).json()
    second = (await client.post(f"/api/v1/projects/{project['id']}/archive", json={"reason": "done again"})).json()
    assert second["archived_at"] == first["archived_at"]


async def test_archiving_requires_a_reason(client: AsyncClient) -> None:
    """NFR-14's "why" is not optional, and an empty string is not a reason."""
    project = await _create(client, "Finished")
    assert (await client.post(f"/api/v1/projects/{project['id']}/archive", json={})).status_code == 422
    assert (await client.post(f"/api/v1/projects/{project['id']}/archive", json={"reason": ""})).status_code == 422


async def test_archiving_writes_an_audit_record_naming_the_actor_and_the_reason(client: AsyncClient) -> None:
    project = await _create(client, "Finished")
    await client.post(f"/api/v1/projects/{project['id']}/archive", json={"reason": "migrated to the monorepo"})

    events = (await client.get(f"/api/v1/audit/events?project_id={project['id']}&limit=50")).json()["events"]
    archived = [e for e in events if e["action"] == "project_archived"]
    assert len(archived) == 1, events
    assert archived[0]["reason"] == "migrated to the monorepo"
    assert archived[0]["actor_kind"] == "user"
    assert archived[0]["actor_user_id"] == str(USER)


async def test_archiving_refuses_another_tenants_project(workspace_app: Any, client: AsyncClient) -> None:
    project = await _create(client, "Private")
    workspace_app.dependency_overrides[require_principal] = lambda: Principal.for_user(
        user_id=USER,
        subject="outsider",
        email="outsider@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=uuid.uuid4(),
    )
    response = await client.post(f"/api/v1/projects/{project['id']}/archive", json={"reason": "not mine"})
    assert response.status_code == 403, response.text


# ─── FR-05: delete ───────────────────────────────────────────────────────────────────────────────


async def test_delete_requires_the_project_name_to_be_typed_back(client: AsyncClient) -> None:
    """A checkbox is clicked by reflex; typing the name proves you know WHICH project this is."""
    project = await _create(client, "Doomed")

    wrong = await client.request(
        "DELETE",
        f"/api/v1/projects/{project['id']}",
        json={"reason": "no longer needed", "confirm_name": "Something Else"},
    )
    assert wrong.status_code == 422, wrong.text
    assert "confirm_name" in wrong.text
    # And it did not delete.
    assert (await client.get(f"/api/v1/projects/{project['id']}")).status_code == 200


async def test_delete_removes_the_project_and_reports_what_cascaded(client: AsyncClient) -> None:
    project = await _create(client, "Doomed")
    await client.put(f"/api/v1/projects/{project['id']}/tags", json={"tag": "temp"})
    await client.put(f"/api/v1/projects/{project['id']}/favourite")

    deleted = await client.request(
        "DELETE",
        f"/api/v1/projects/{project['id']}",
        json={"reason": "no longer needed", "confirm_name": "Doomed"},
    )
    assert deleted.status_code == 200, deleted.text
    report = deleted.json()
    # The counts are the honest scope of the operation, not a 204 that says nothing.
    assert report["cascaded"]["project_tags"] == 1
    assert report["cascaded"]["project_favourites"] == 1
    # Every table with a cascading reference is enumerated, so a caller can see the whole blast
    # radius rather than the two rows this test happened to create.
    assert "file_tree" in report["cascaded"]
    assert "change_sets" in report["cascaded"]
    assert "embeddings" in report["cascaded"]

    assert (await client.get(f"/api/v1/projects/{project['id']}")).status_code == 403


async def test_delete_preserves_the_audit_trail(client: AsyncClient) -> None:
    """Revision `0007`'s stated intent, finally asserted.

    `audit_events.project_id` carries no foreign key so "an immutable log that cascades away when a
    project is deleted is not an immutable log". The table also REVOKEs UPDATE and DELETE from the
    application role, so there is no statement this route could issue to remove or blank these rows.
    The events therefore keep the id of a project that no longer exists, which is the honest record.
    """
    project = await _create(client, "Doomed")
    await client.post(f"/api/v1/projects/{project['id']}/archive", json={"reason": "before deletion"})
    await client.post(f"/api/v1/projects/{project['id']}/unarchive", json={"reason": "briefly restored"})

    deleted = await client.request(
        "DELETE",
        f"/api/v1/projects/{project['id']}",
        json={"reason": "no longer needed", "confirm_name": "Doomed"},
    )
    assert deleted.status_code == 200, deleted.text
    # Two archive events plus the deletion's own record, all counted before the row went.
    assert deleted.json()["audit_events_retained"] >= 2

    survivors = (await client.get(f"/api/v1/audit/events?project_id={project['id']}&limit=50")).json()["events"]
    actions = {e["action"] for e in survivors}
    assert {"project_archived", "project_unarchived", "project_deleted"} <= actions
    # The deletion's own record carries what was lost, so the scope is recoverable from the log.
    record = next(e for e in survivors if e["action"] == "project_deleted")
    assert record["before_state"]["name"] == "Doomed"
    assert record["reason"] == "no longer needed"


async def test_delete_refuses_another_tenants_project(workspace_app: Any, client: AsyncClient) -> None:
    project = await _create(client, "Private")
    workspace_app.dependency_overrides[require_principal] = lambda: Principal.for_user(
        user_id=USER,
        subject="outsider",
        email="outsider@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=uuid.uuid4(),
    )
    response = await client.request(
        "DELETE",
        f"/api/v1/projects/{project['id']}",
        json={"reason": "not mine", "confirm_name": "Private"},
    )
    # 403 before the name check, so the response cannot confirm another tenant's project name.
    assert response.status_code == 403, response.text
