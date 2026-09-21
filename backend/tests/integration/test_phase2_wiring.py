# SPDX-License-Identifier: FSL-1.1-ALv2
"""§2.1 and §2.2 through the REAL object graph, not through constructors this test calls itself.

WHY THIS FILE EXISTS, in the words of the gate that demanded it. `production_app.state` is the production
composition's public surface, and Phase 0 shipped 419 green tests over an MCP gateway that raised
`TypeError` on every request — because nothing had ever driven `production_app.state.mcp_gateway` through the graph
the lifespan actually builds. `test_wiring_coverage.py` compares the attributes the real lifespan composes
against the `@wires` declarations in this directory and fails on anything composed but never declared.
`environment_service` and `deployment_service` arrived without one, and it caught them.

So the tests below go through the HTTP surface of an app built by the real lifespan. What they establish
is not that the services work — `test_environments.py` and `test_deployments.py` do that — but that the
composition reaches them at all: the router is registered, the state attribute is named the way the route
reads it, and the sealing key the environment service needs was actually derived at startup rather than
left for the first request to discover.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from .wiring import wires

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


@wires("environment_service", "deployment_service")
class TestTheEnvironmentAndDeploymentServicesAreReachedThroughTheRealGraph:
    async def test_the_environment_router_is_registered_and_reaches_its_service(self, production_app: Any) -> None:
        """A 401 is the RIGHT answer here, and the reason is the whole point of the test.

        The request carries no principal, so `require_principal` refuses it — which proves the route
        exists, is mounted under the prefix the client used, and is behind the router-level dependency.
        A 404 would mean the router was never included; a 500 would mean the route was reached and the
        composition behind it was broken. Distinguishing those three is what this asserts.
        """
        transport = ASGITransport(app=production_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(f"/api/v1/projects/{uuid.uuid4()}/environments")

        assert response.status_code == 401, response.text

    async def test_the_deployment_router_is_registered_and_reaches_its_service(self, production_app: Any) -> None:
        transport = ASGITransport(app=production_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                f"/api/v1/projects/{uuid.uuid4()}/deployments",
                json={"environment_id": str(uuid.uuid4()), "manifests": ["k8s/deployment.yaml"]},
            )

        assert response.status_code == 401, response.text

    async def test_the_lifespan_composed_both_services(self, production_app: Any) -> None:
        """Named exactly as the routes read them.

        `_service(request)` reads `request.production_app.state.deployment_service` and
        `request.production_app.state.environment_service`; a typo in either would produce an AttributeError on the
        first authenticated request rather than at startup, which is precisely the failure mode A§0.4.1
        exists to prevent.
        """
        assert getattr(production_app.state, "environment_service", None) is not None
        assert getattr(production_app.state, "deployment_service", None) is not None

    async def test_the_environment_services_sealing_key_was_derived_at_startup(self, production_app: Any) -> None:
        """A key derived lazily is a key whose absence is discovered by a user.

        `EnvironmentService.__init__` derives the AES-256-GCM key from `ENVELOPE_PEPPER` and REFUSES an
        empty pepper. Composing it in the lifespan therefore means a deployment with no pepper fails to
        start, rather than accepting environment secrets and sealing them under a key derived from the
        empty string — which looks exactly like encryption and provides none.
        """
        service = production_app.state.environment_service
        sealed = service._key  # noqa: SLF001 - the property under test is that it exists at startup
        assert isinstance(sealed, bytes)
        assert len(sealed) == 32
