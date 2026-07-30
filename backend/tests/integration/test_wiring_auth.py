# SPDX-License-Identifier: FSL-1.1-ALv2
"""The two verifiers, driven through the REAL object graph (§0.4.1, §11.2, §4.4).

`test_wiring_coverage.py` requires every attribute `create_app()` composes to be named
by a `@wires(...)` declaration in some wiring test. That rule is what stopped Phase 0's
central mistake — a component composed, green in unit tests, and never once exercised
through the graph uvicorn actually runs (D-23). Adding `app_token_verifier` and
`token_verifier` to the app factory therefore obliges this file to exist.

What is asserted here is the one thing unit tests cannot: that the app the factory
builds has TWO verifiers with DIFFERENT audiences, and that they are not the same
object. A single shared verifier would pass every unit test in
`test_auth_verifier.py` — those construct their own instance — while quietly removing
the RFC 9207 mix-up defence from the running server.
"""

from __future__ import annotations

import pytest

from .wiring import wires

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]


@wires("app_token_verifier", "token_verifier")
class TestTheTwoVerifiersAreDistinct:
    async def test_both_are_composed(self, production_app) -> None:
        assert getattr(production_app.state, "app_token_verifier", None) is not None
        assert getattr(production_app.state, "token_verifier", None) is not None

    async def test_they_are_not_the_same_object(self, production_app) -> None:
        """One shared verifier would pass every unit test and silently drop the mix-up
        defence from the running server."""
        assert production_app.state.app_token_verifier is not production_app.state.token_verifier

    async def test_their_audiences_differ(self, production_app) -> None:
        """The enforceable half of RFC 9207 at a resource server: a token minted for the
        MCP gateway must not verify against the product API, and vice versa."""
        app_audience = production_app.state.app_token_verifier._audience  # noqa: SLF001
        gateway_audience = production_app.state.token_verifier._audience  # noqa: SLF001
        assert app_audience, "the product API audience is empty"
        assert gateway_audience, "the gateway audience is empty"
        assert app_audience != gateway_audience

    async def test_the_gateway_alias_points_at_the_gateway_verifier(self, production_app) -> None:
        """`token_verifier` is an alias so `require_mcp_principal` need not know it is
        the MCP verifier. The alias must actually alias it, or the MCP surface would be
        verifying against the wrong audience while looking correct."""
        assert production_app.state.token_verifier is production_app.state.mcp_verifier

    async def test_the_product_verifier_reports_one_problem_type(self, production_app) -> None:
        """Every failure mode maps to the single registered `unauthenticated` type, so a
        401 body cannot tell a caller which check failed."""
        verifier = production_app.state.app_token_verifier
        assert set(verifier.problem_types.values()) == {"unauthenticated"}


@wires("app_token_verifier")
class TestDenyByDefaultOverTheRealRouter:
    async def test_an_unauthenticated_protected_route_is_401(self, production_app) -> None:
        """Through the composed app, not a hand-built one. `/api/v1/analysis/plan` is
        not in `PUBLIC_ROUTES`, so it must reject a request with no Authorization
        header before the handler runs."""
        import httpx

        transport = httpx.ASGITransport(app=production_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/v1/analysis/plan", json={})
        assert response.status_code == 401, response.text

    async def test_a_public_route_still_serves_without_a_token(self, production_app) -> None:
        """The other half. Deny-by-default that also denied `/health` would break the
        container liveness contract, and the failure would look like an outage."""
        import httpx

        transport = httpx.ASGITransport(app=production_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/health")
        assert response.status_code == 200, response.text
