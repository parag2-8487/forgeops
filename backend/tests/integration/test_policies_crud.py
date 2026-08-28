# SPDX-License-Identifier: FSL-1.1-ALv2
"""The policy surface: CRUD, the list route, tenant confinement and an honest dry-run.

Three things about this module changed with the policy management UI, and each was a defect the old
version of this file actively concealed.

**The principal was a `CerbosPrincipal`, which has no `tenant_id`.** So none of these tests could
observe row visibility, and the handlers did not scope on it either — `session.get(Policy, id)` reads
any tenant's row. `test_projects_api.py` had already recorded this exact substitution as a mistake
("a test that passed with an object lacking the field it scopes on was proving nothing about
scoping"); this module kept it. It is a real `Principal` now, and `test_another_tenants_policy_is_not
_readable` is the case that could not previously exist.

**A missing policy answered 404, which is an enumeration oracle.** It is the non-disclosing 403 now,
byte-identical whether the policy is absent or belongs to another tenant (§4.2, Q-20).

**The dry-run test asserted the FABRICATED answer.** The route returned
`"allow" if input["action"] == "allow_me" else "deny"` when the `opa` binary was absent, and the test
asserted precisely `allow` for `allow_me` and `deny` for anything else — the synthesised values. So
it passed identically with OPA installed and with OPA missing, and could not distinguish a working
policy engine from no policy engine at all. That is the "test that cannot fail" shape. It now gates
on the `opa` capability, asserts the evaluator's version reached the response, and has a separate
case proving the binary's ABSENCE produces a problem document rather than a decision.
"""

from __future__ import annotations

import shutil
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

from tests.integration.capability import require_capability
from tests.integration.production_app import apply_committed_baseline_env

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

TENANT = uuid.UUID("55555555-5555-5555-5555-555555555555")
OTHER_TENANT = uuid.UUID("66666666-6666-6666-6666-666666666666")
USER = uuid.UUID("44444444-4444-4444-4444-444444444444")

#: Names this module creates, so teardown removes exactly its own rows rather than emptying the
#: table. A `DELETE FROM policies` would pass and would also destroy anything another module left
#: behind, which is how a suite becomes order-dependent.
OWNED_NAMES = (
    "Test Policy",
    "Updated Policy",
    "Dry Run Policy",
    "Listed Policy A",
    "Listed Policy B",
    "Other Tenant Policy",
)


def _principal(tenant_id: uuid.UUID = TENANT) -> Principal:
    return Principal.for_user(
        user_id=USER,
        subject="policies-test",
        email="policy-author@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=tenant_id,
    )


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
    app.dependency_overrides[require_principal] = _principal

    async with LifespanManager(app):
        yield app

    app.dependency_overrides.clear()

    engine = create_async_engine(schema_at_head)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM policies WHERE name = ANY(:names)"), {"names": list(OWNED_NAMES)})
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(policies_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=policies_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _rego(package: str = "forgeops", rule: str = "default allow = true") -> str:
    return f"package {package}\n\n{rule}\n"


async def test_policy_crud_lifecycle(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/policies",
        json={"name": "Test Policy", "rego_rules": _rego(), "enabled": True, "engine": "rego"},
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "Test Policy"
    assert created["enabled"] is True
    # The timestamps the response model used to drop. Database-generated, so their presence is
    # evidence of a row rather than of a constructed reply — and the list screen orders on them.
    assert created["created_at"] is not None
    assert created["updated_at"] is not None
    policy_id = created["id"]

    resp = await client.get(f"/api/v1/policies/{policy_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == policy_id

    resp = await client.patch(f"/api/v1/policies/{policy_id}", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    resp = await client.delete(f"/api/v1/policies/{policy_id}")
    assert resp.status_code == 204

    # 403, not 404. A deleted policy and another tenant's policy must answer identically or the
    # status code enumerates ids.
    resp = await client.get(f"/api/v1/policies/{policy_id}")
    assert resp.status_code == 403, resp.text


async def test_the_list_route_enumerates_stored_policies(client: AsyncClient) -> None:
    """`GET /api/v1/policies` — the route whose absence made a policy screen unbuildable.

    Publish, templates, create, read, update, delete and test all existed. Every one of them except
    templates needs an id you already have, and nothing returned one, so the only enumerable thing
    was the immutable template list.
    """
    for name in ("Listed Policy A", "Listed Policy B"):
        created = await client.post("/api/v1/policies", json={"name": name, "rego_rules": _rego()})
        assert created.status_code == 201, created.text

    resp = await client.get("/api/v1/policies?limit=100")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = [p["name"] for p in body["policies"]]
    assert "Listed Policy A" in names
    assert "Listed Policy B" in names
    # Newest first, which is what the cursor orders on.
    assert names.index("Listed Policy B") < names.index("Listed Policy A")
    assert body["next_cursor"] is None


async def test_the_list_route_pages_by_cursor_without_repeating_a_row(client: AsyncClient) -> None:
    """A keyset page, and the cursor round-trips through a URL.

    The cursor is base64url rather than `"<iso>|<uuid>"` because an ISO timestamp contains `+00:00`
    and `+` decodes to a space in a query string, so a raw cursor arrives unparseable. Asserted by
    actually sending it as a query parameter rather than by inspecting its shape.
    """
    for name in ("Listed Policy A", "Listed Policy B"):
        assert (await client.post("/api/v1/policies", json={"name": name, "rego_rules": _rego()})).status_code == 201

    first = (await client.get("/api/v1/policies?limit=1")).json()
    assert len(first["policies"]) == 1
    assert first["next_cursor"] is not None

    second = (await client.get(f"/api/v1/policies?limit=1&cursor={first['next_cursor']}")).json()
    assert len(second["policies"]) == 1
    assert second["policies"][0]["id"] != first["policies"][0]["id"]


async def test_a_malformed_cursor_is_a_named_validation_failure(client: AsyncClient) -> None:
    """Not an empty page. An empty page looks like an answer and loops a paging client forever."""
    resp = await client.get("/api/v1/policies?cursor=this-is-not-base64url-of-anything")
    assert resp.status_code == 422, resp.text
    assert "cursor" in resp.text


async def test_the_list_route_filters_by_enabled(client: AsyncClient) -> None:
    created = (
        await client.post("/api/v1/policies", json={"name": "Listed Policy A", "rego_rules": _rego(), "enabled": False})
    ).json()

    disabled = (await client.get("/api/v1/policies?enabled=false&limit=100")).json()
    assert created["id"] in [p["id"] for p in disabled["policies"]]

    enabled = (await client.get("/api/v1/policies?enabled=true&limit=100")).json()
    assert created["id"] not in [p["id"] for p in enabled["policies"]]


async def test_another_tenants_policy_is_neither_listed_nor_readable(
    policies_app: Any, client: AsyncClient, schema_at_head: str
) -> None:
    """The case the `CerbosPrincipal` stand-in made impossible to write.

    `session.get(Policy, policy_id)` reads by primary key with no tenant predicate, so before this
    any authenticated caller could read, rewrite or delete another tenant's governance rules by id.
    """
    other_id = uuid.uuid4()
    engine = create_async_engine(schema_at_head)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO policies (id, tenant_id, name, engine, rego_rules, enabled) "
                    "VALUES (:id, :tenant, :name, 'rego', :rego, true)"
                ),
                {"id": other_id, "tenant": OTHER_TENANT, "name": "Other Tenant Policy", "rego": _rego()},
            )
    finally:
        await engine.dispose()

    listed = (await client.get("/api/v1/policies?limit=100")).json()
    assert str(other_id) not in [p["id"] for p in listed["policies"]]

    # Read, write and delete all refuse, and all with the same non-disclosing body.
    read = await client.get(f"/api/v1/policies/{other_id}")
    assert read.status_code == 403, read.text
    forbidden_body = read.json()

    patched = await client.patch(f"/api/v1/policies/{other_id}", json={"enabled": False})
    assert patched.status_code == 403
    deleted = await client.delete(f"/api/v1/policies/{other_id}")
    assert deleted.status_code == 403

    # A policy id that never existed answers identically, which is what makes the 403 useless as an
    # oracle. `instance` and `trace_id` are per-request, so they are excluded from the comparison.
    absent = await client.get(f"/api/v1/policies/{uuid.uuid4()}")
    assert absent.status_code == 403
    strip = ("instance", "trace_id")
    assert {k: v for k, v in absent.json().items() if k not in strip} == {
        k: v for k, v in forbidden_body.items() if k not in strip
    }


async def test_policy_templates(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/policies/templates")
    assert resp.status_code == 200
    templates = resp.json()
    assert isinstance(templates, list)
    assert any(t["id"] == "scheduling" for t in templates)
    assert any(t["id"] == "file_restrictions" for t in templates)


async def test_policy_create_invalid_rego(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/policies",
        json={"name": "Invalid Policy", "rego_rules": "package forgeops\ndefault allow == true", "enabled": True},
    )
    assert resp.status_code == 422
    assert "rego_parse_error" in resp.text


async def test_the_dry_run_returns_a_decision_opa_actually_computed(client: AsyncClient) -> None:
    """A real evaluation, attributed to a real evaluator.

    Gated on the `opa` BINARY rather than on the OPA server: the policy under test has not been
    published to any bundle, so there is nothing a server could have loaded. `require_capability`
    fails rather than skips under `FORGEOPS_REQUIRE_INTEGRATION`, so this cannot go back to being a
    test that passes because the answer was invented.
    """
    if shutil.which("opa") is None:
        require_capability("opa", "the `opa` binary is not on PATH")

    created = await client.post(
        "/api/v1/policies",
        json={
            "name": "Dry Run Policy",
            "rego_rules": (
                "package forgeops.governance\n\n"
                'default decision = "deny"\n\n'
                'decision = "allow" if { input.action == "allow_me" }\n'
            ),
        },
    )
    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]

    allowed = await client.post(f"/api/v1/policies/{policy_id}/test", json={"input": {"action": "allow_me"}})
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["decision"] == "allow"
    assert body["undefined"] is False
    # The two fields that make the answer attributable. A decision with no statement of what produced
    # it is indistinguishable from the fabricated one this replaces.
    assert body["rule"] == "data.forgeops.governance.decision"
    assert body["evaluated_with"].startswith("opa "), body["evaluated_with"]

    denied = await client.post(f"/api/v1/policies/{policy_id}/test", json={"input": {"action": "deny_me"}})
    assert denied.status_code == 200, denied.text
    assert denied.json()["decision"] == "deny"


async def test_the_dry_run_reports_undefined_rather_than_calling_it_a_deny(client: AsyncClient) -> None:
    """A rule that never matched is not a rule that refused.

    The old code defaulted to `"deny"` when `opa eval` returned no result set, which hid a policy
    that does not define the decision rule behind a verdict that looks like enforcement — the single
    most misleading thing this surface could say to someone testing a rule they had misspelled.
    """
    if shutil.which("opa") is None:
        require_capability("opa", "the `opa` binary is not on PATH")

    created = await client.post(
        "/api/v1/policies",
        # A valid policy in a DIFFERENT package, so `data.forgeops.governance.decision` is undefined.
        json={"name": "Dry Run Policy", "rego_rules": _rego(package="forgeops.unrelated")},
    )
    assert created.status_code == 201, created.text

    resp = await client.post(f"/api/v1/policies/{created.json()['id']}/test", json={"input": {"action": "allow_me"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["undefined"] is True
    assert resp.json()["decision"] == "undefined"


async def test_the_dry_run_refuses_rather_than_fabricating_when_opa_is_absent(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, asserted directly.

    `shutil.which` is forced to find nothing, which is the exact condition under which the route used
    to answer `{"decision": "allow"}` for `action == "allow_me"` — a verdict on a security surface
    that no policy engine had computed. Monkeypatched rather than gated on the environment, so this
    runs on every machine including the ones that DO have OPA installed; a test of the absent-binary
    path that only ran where the binary was absent would never run in CI, which is where it matters.
    """
    created = await client.post("/api/v1/policies", json={"name": "Dry Run Policy", "rego_rules": _rego()})
    assert created.status_code == 201, created.text

    monkeypatch.setattr("src.policies.routes.shutil.which", lambda _name: None)

    resp = await client.post(f"/api/v1/policies/{created.json()['id']}/test", json={"input": {"action": "allow_me"}})
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["type"].endswith("/dryrun-unavailable")
    # Names the missing binary, so the failure is actionable rather than mysterious.
    assert "opa" in (body.get("detail") or "")
    # And carries NO decision. The whole point is that there is nothing to report.
    assert "decision" not in body
