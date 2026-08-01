# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent pairing surface, composed (§0.4.1, §3.1, §4.4, §11.2, §14.2).

Why this file exists
--------------------
§0.4.1's rule is that a collaborator the lifespan places on `app.state` must be driven through the
**real object graph** by some test, and `test_wiring_coverage.py` enforces it. `device_ca` arrives
with leaf 8.2, so it needs a `@wires` declaration — but the declaration is the smaller half. The
larger half is D-23's lesson: Phase 0 shipped registered routes whose composition was never
assembled, and every request to them raised `AttributeError`. Three new routes land here, so three
new routes get asserted against `create_app()`'s real router rather than against a hand-built one.

What is asserted, and what is left to the integration tests
----------------------------------------------------------
This file answers "is it composed and reachable". `test_agent_pairing.py` answers "does it behave".
Keeping the split means a composition break shows up as a failure *here*, naming the wiring, rather
than as thirty behavioural failures whose common cause a reader has to infer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI

from .production_app import production_app  # noqa: F401 - fixture
from .wiring import wires

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

CHECKER = Path(__file__).resolve().parents[3] / "scripts" / "check-route-auth.py"


def _load_route_checker() -> ModuleType:
    """Import `scripts/check-route-auth.py` by path.

    By path because its filename has a hyphen and is therefore not importable as a module name.
    The same module the `backend` CI job runs, so the test's notion of "a registered route" cannot
    drift from the gate's.
    """
    spec = importlib.util.spec_from_file_location("forgeops_route_auth_checker", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: The three routes §11.2 lists, with whether each requires a principal (§4.4).
EXPECTED_ROUTES = {
    ("/api/v1/agents/pairing-codes", "POST"): True,
    ("/api/v1/agents/pair/exchange", "POST"): False,
    ("/api/v1/agents/{device_id}", "DELETE"): True,
}


@wires("device_ca")
class TestTheAgentSurfaceIsComposed:
    async def test_the_ca_is_on_app_state_and_is_one_of_the_two_permitted_shapes(self, production_app: FastAPI) -> None:  # noqa: F811
        """Either a real CA or the fail-closed stand-in — never `None`, never something else.

        Both shapes are acceptable and which one appears depends on whether `INTERNAL_CA_CERT_PEM`
        is configured in the environment the tests run in, so the assertion is on the Protocol
        rather than on the concrete class. What it excludes is the third possibility: an attribute
        that was never set, which is exactly D-23's failure.
        """
        from src.auth.ca import CertificateIssuer

        assert isinstance(production_app.state.device_ca, CertificateIssuer)

    async def test_the_device_service_holds_the_composed_ca(self, production_app: FastAPI) -> None:  # noqa: F811
        """Not a second CA. Two CAs would issue certificates the hub's chain check rejects."""
        service = production_app.state.device_service
        assert service._ca is production_app.state.device_ca  # noqa: SLF001 - composition assertion

    async def test_the_device_service_holds_the_composed_audit_writer(self, production_app: FastAPI) -> None:  # noqa: F811
        """D-70's recorder must wrap the app's writer, or the pairing chain forks from the transit
        chain under concurrency — the same reasoning `test_wiring_governance.py` applies to the
        chokepoint."""
        service = production_app.state.device_service
        assert service._recorder._writer is production_app.state.audit_writer  # noqa: SLF001

    async def test_the_device_service_holds_the_composed_redis_client(self, production_app: FastAPI) -> None:  # noqa: F811
        """The consume script's atomicity is per Redis instance; a second client to a second
        instance would make single-use true only within one process."""
        service = production_app.state.device_service
        assert service._redis is production_app.state.redis  # noqa: SLF001

    async def test_all_three_routes_are_registered_on_the_real_app(self, production_app: FastAPI) -> None:  # noqa: F811
        """Enumerated through `check-route-auth.py`'s own flattener, not through `app.routes`.

        FastAPI 0.139 does not flatten `include_router` into `app.routes`: each inclusion appears as
        an `_IncludedRouter` wrapper with no `path` attribute, so a naive walk over `app.routes`
        finds **nothing** and this clause would have passed vacuously for an empty set. Reusing the
        checker's flattener means the test and the gate agree about what "a registered route" is —
        the Q-06/Q-14 lesson applied to a route inventory.
        """
        flatten = _load_route_checker()._flatten  # noqa: SLF001 - the checker's own enumeration
        registered = {
            (prefix + route.path, method)
            for prefix, route in flatten(production_app.routes)
            for method in getattr(route, "methods", set()) or set()
            if (prefix + route.path).startswith("/api/v1/agents")
        }
        assert registered == set(EXPECTED_ROUTES), registered

    async def test_only_the_exchange_is_public(self, production_app: FastAPI) -> None:  # noqa: F811
        """§4.4's set, checked against the router rather than against the document.

        `check-route-auth.py` performs the same comparison for every route in the app; this states
        it for the three that landed with this group, so a regression here fails in the file that
        explains why.
        """
        from src.auth.dependencies import route_requires_principal

        for (path, method), needs_principal in EXPECTED_ROUTES.items():
            assert route_requires_principal(path, {method}) is needs_principal, (path, method)

    async def test_the_exchange_route_serves_and_does_not_require_a_token(self, production_app: FastAPI) -> None:  # noqa: F811
        """The self-clearing `arrives_in` marker's other half: the route must actually serve.

        A 422 for an empty body is the correct answer here and is what proves the point — the
        request reached FastAPI's validation of `ExchangeRequest`, which means it was not refused
        by the auth dependency first. A 401 would mean the route is not public; a 404 would mean it
        is not registered at all.
        """
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=production_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/v1/agents/pair/exchange", json={})
        assert response.status_code == 422, response.text

    async def test_the_issue_route_refuses_an_unauthenticated_caller(self, production_app: FastAPI) -> None:  # noqa: F811
        """The control for the clause above: the same shape of request, on the protected route."""
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=production_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/v1/agents/pairing-codes", json={})
        assert response.status_code == 401, response.text
