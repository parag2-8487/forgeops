# SPDX-License-Identifier: FSL-1.1-ALv2
"""The audit surface over the REAL object graph (design.md §0.4.1, §11.9, criterion 9).

§0.4.1's clause 1: every component composed in production has at least one test that instantiates
the **real** collaborators exactly as `create_app()` does and drives it through the real route.
`app.state.audit_writer` is composed by the lifespan, so it needs this file — and
`test_wiring_coverage.py` fails the build if it does not exist, which is the mechanism rather than
the intention.

Only transports are substituted here, never collaborators: `production_app` points the app at
unreachable Postgres and Redis URLs. That is why these tests assert **routing, authentication and
authorisation** rather than chain arithmetic — the database is deliberately absent. The chain is
asserted against a real PostgreSQL in `test_audit_writer.py`, which is the correct division:
this file proves the surface is reachable and guarded, that one proves it is correct.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from .production_app import production_app  # noqa: F401 - fixture
from .wiring import wires

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

AUDIT_ROUTES = ("/api/v1/audit/events", "/api/v1/audit/verify")


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@wires("audit_writer")
class TestTheAuditSurfaceIsComposedAndGuarded:
    async def test_the_writer_is_on_app_state(self, production_app: FastAPI) -> None:  # noqa: F811
        """The composition, asserted against the real lifespan rather than a constructor call."""
        from src.audit.writer import AuditWriter

        writer = production_app.state.audit_writer
        assert isinstance(writer, AuditWriter)

    async def test_the_writer_uses_the_configured_lock_key(self, production_app: FastAPI) -> None:  # noqa: F811
        """The lock key is configuration (`AUDIT_ADVISORY_LOCK_KEY`), so the running app must be
        derived from it rather than from a default baked into the class — the same provenance
        question Q-27 asks of the tier config."""
        settings = production_app.state.settings
        assert production_app.state.audit_writer.advisory_lock_key == settings.audit_advisory_lock_key

    async def test_both_routes_are_registered_on_the_real_app(self, production_app: FastAPI) -> None:  # noqa: F811
        """Read from the OpenAPI schema, because FastAPI 0.139 does not flatten `include_router`
        into `app.routes` — the defect the journal records as finding 5, which made
        `check-route-auth.py` examine three health endpoints and report success."""
        paths = set(production_app.openapi()["paths"])
        for route in AUDIT_ROUTES:
            assert route in paths, f"{route} is not registered on the real app"

    @pytest.mark.parametrize("route", AUDIT_ROUTES)
    async def test_an_unauthenticated_request_is_refused(self, production_app: FastAPI, route: str) -> None:  # noqa: F811
        """Deny by default (§4.4, Q-19). Asserted per route rather than by reading the router's
        dependency list, because the question is what an unauthenticated caller receives."""
        async with await _client(production_app) as client:
            response = await client.get(route)
        assert response.status_code == 401, response.text
        assert response.headers["content-type"].startswith("application/problem+json")

    @pytest.mark.parametrize("route", AUDIT_ROUTES)
    async def test_neither_route_is_public(self, production_app: FastAPI, route: str) -> None:  # noqa: F811
        """`PUBLIC_ROUTES` is the only legitimate exemption, and an audit log is never in it."""
        from src.auth.public_routes import is_public

        assert not is_public(route, "GET")

    async def test_there_is_no_write_endpoint(self, production_app: FastAPI) -> None:  # noqa: F811
        """A route that could post an audit record would be a route that could forge one. Records
        come from governance transits (§11.6) and from the hub, never from a client."""
        schema = production_app.openapi()["paths"]
        for path, operations in schema.items():
            if not path.startswith("/api/v1/audit"):
                continue
            assert set(operations) <= {"get"}, f"{path} exposes {sorted(operations)}; the log is append-only"

    async def test_verify_is_narrowed_to_admin(self, production_app: FastAPI) -> None:  # noqa: F811
        """Not because the result is sensitive — it is a hash comparison — but because an unbounded
        recomputation available to any authenticated caller is a cheap way to make the database
        everybody's problem.

        Asserted by finding the route object on the real app and inspecting its dependant tree for
        the role gate's qualified name, which is the same identification `check-route-auth.py`
        uses. Comparing against `AUTH_DEPENDENCY_QUALNAMES` rather than a substring means a renamed
        dependency fails here instead of silently matching.
        """
        from src.auth.dependencies import AUTH_DEPENDENCY_QUALNAMES

        route = _find_route(production_app, "/api/v1/audit/verify")
        names = {
            dependency.call.__qualname__ for dependency in route.dependant.dependencies if dependency.call is not None
        }
        assert "require_role.<locals>.dependency" in names, (
            f"/verify carries {sorted(names)}; the admin gate is missing"
        )
        assert "require_role.<locals>.dependency" in AUTH_DEPENDENCY_QUALNAMES

    async def test_the_events_route_takes_no_tenant_parameter(self, production_app: FastAPI) -> None:  # noqa: F811
        """Tenant scope comes from the principal. A `tenant_id` query argument would be a
        cross-tenant read waiting for a caller to try it, and D-35 leaves the column nullable with
        no RLS policy behind it in Phase 1 — so the confinement has to be in the handler."""
        schema = production_app.openapi()["paths"]["/api/v1/audit/events"]["get"]
        parameters = {parameter["name"] for parameter in schema.get("parameters", [])}
        assert "tenant_id" not in parameters, parameters
        # And the filters that SHOULD be there, so this is not vacuous.
        assert {"project_id", "actor_kind", "action", "outcome", "cursor", "limit"} <= parameters


def _find_route(app: FastAPI, path: str) -> object:
    """Locate a route on the real app, descending into included routers.

    FastAPI 0.139 keeps each `include_router` as one opaque entry rather than flattening it, so a
    top-level scan finds nothing. Duck-typed on `original_router` rather than importing the private
    class, so a renamed internal degrades to "route not found" — which this function then raises
    on, instead of silently returning None and making every assertion above pass.
    """
    pending = list(app.routes)
    while pending:
        candidate = pending.pop()
        inner = getattr(candidate, "original_router", None)
        if inner is not None:
            pending.extend(inner.routes)
            continue
        if getattr(candidate, "path", None) == path and hasattr(candidate, "dependant"):
            return candidate
    raise AssertionError(f"route {path} not found on the real app")
