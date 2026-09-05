# SPDX-License-Identifier: FSL-1.1-ALv2
"""`GET /api/v1/ai/tiers` is a read a HUMAN performs, and must authenticate as one.

It used to sit on the completion router and inherit `require_mcp_principal`, which demands the GATEWAY
audience. A browser session token cannot satisfy that, so the Models screen showed

    Not authenticated to read model tiers.

to a correctly signed-in user, permanently — signing in again could never help, because the token was
being REFUSED rather than found missing. The panel's own diagnosis said as much and pointed at
`OIDC_APP_AUDIENCE`, which sent the reader after a configuration problem that did not exist.

The fix moved the route to a second router requiring a USER principal. These assert the three facts that
matter: a user may read it, an anonymous caller may not, and the completion endpoint's contract did not
move to make the read work.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from src.auth.dependencies import require_mcp_principal, require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal


@pytest_asyncio.fixture
async def app_no_auth(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """The REAL app under its committed baseline env, with auth left in place.

    Nothing here disables authentication; the tests override exactly one dependency each so that what
    is being proved is which principal a route accepts, not whether the route runs.
    """
    from src.main import create_app

    from tests.integration.production_app import apply_committed_baseline_env

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app()
    async with LifespanManager(app):
        yield app


TIERS_PATH = "/api/v1/ai/tiers"
COMPLETE_PATH = "/api/v1/ai/complete"
USER = uuid.uuid4()


def _user_principal() -> Principal:
    """An ordinary signed-in developer — deliberately NOT an admin.

    Reading which model tier a request would reach is not a privileged operation, and a test that
    proved it only for an admin would leave the common case unproven.
    """
    return Principal.for_user(
        user_id=USER,
        subject="tiers-read-test",
        email="dev@forgeops.invalid",
        role=UserRole.DEVELOPER,
    )


@pytest.mark.asyncio
async def test_a_signed_in_user_can_read_the_tier_map(app_no_auth) -> None:
    """The bug, directly: a user principal must reach the handler rather than bounce off the audience.

    Asserted on the REAL app with only `require_principal` satisfied — nothing overrides
    `require_mcp_principal`, so if the route were still on the completion router this would 401 exactly
    as the screen did.
    """
    app_no_auth.dependency_overrides[require_principal] = _user_principal
    try:
        transport = ASGITransport(app=app_no_auth)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(TIERS_PATH)
    finally:
        app_no_auth.dependency_overrides.pop(require_principal, None)

    assert response.status_code == 200, (
        f"a signed-in user got {response.status_code} reading the tier map; this is the Models screen's "
        "'Not authenticated to read model tiers.'"
    )
    body = response.json()
    assert "tiers" in body
    # Every tier reports its breaker state, which is the whole point of the panel: a tier that exists
    # and a tier that is currently letting traffic through are different facts.
    for tier in body["tiers"]:
        assert tier["breaker_state"], f"tier {tier.get('name')!r} reported no breaker state"
        assert "available" in tier


@pytest.mark.asyncio
async def test_an_anonymous_caller_is_still_refused(app_no_auth) -> None:
    """Deny-by-default is preserved. The route moved to a different principal, not to none."""
    transport = ASGITransport(app=app_no_auth)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(TIERS_PATH)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_the_completion_endpoint_still_demands_the_gateway_audience(app_no_auth) -> None:
    """The read was moved OFF the strict router; the strict router was not loosened.

    This is the assertion that stops the easy wrong fix. Satisfying `require_principal` alone must not
    open `/complete` — a user session token is not a gateway token, and §4.4 puts the completion
    surface on the same contract as MCP.
    """
    app_no_auth.dependency_overrides[require_principal] = _user_principal
    try:
        transport = ASGITransport(app=app_no_auth)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                COMPLETE_PATH, json={"tier": "high_coding", "prompt": "hello"}
            )
    finally:
        app_no_auth.dependency_overrides.pop(require_principal, None)

    assert response.status_code == 401, (
        f"POST /complete answered {response.status_code} to a caller holding only a USER principal; "
        "the gateway-audience contract has been widened, which is the fix this change avoided"
    )


@pytest.mark.asyncio
async def test_the_two_routers_share_one_prefix(app_no_auth) -> None:
    """The API surface did not move. `/api/v1/ai/tiers` is where it always was.

    A split that changed the path would have broken the Models screen a second way while fixing the
    first, and the frontend reads this exact string.
    """
    # Read from the OPENAPI SCHEMA rather than `app.routes`, because routers mount during the
    # lifespan and the schema is the surface a client actually discovers.
    paths = app_no_auth.openapi()["paths"]
    assert TIERS_PATH in paths, f"the tier read moved; paths were {sorted(paths)}"
    assert "get" in paths[TIERS_PATH]
    assert COMPLETE_PATH in paths
    assert "post" in paths[COMPLETE_PATH]


@pytest.mark.asyncio
async def test_the_gateway_may_also_still_read_the_tier_map(app_no_auth) -> None:
    """A gateway did not LOSE the read when it moved.

    `require_principal` accepts a machine principal too, so the MCP gateway that could read tiers before
    can still read them. Worth pinning: a fix that traded one caller for another would be a regression
    wearing a fix's clothes.
    """

    def _gateway() -> Principal:
        return Principal.for_user(
            user_id=USER,
            subject="mcp-gateway",
            email="gateway@forgeops.invalid",
            role=UserRole.ADMIN,
        )

    app_no_auth.dependency_overrides[require_principal] = _gateway
    app_no_auth.dependency_overrides[require_mcp_principal] = _gateway
    try:
        transport = ASGITransport(app=app_no_auth)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(TIERS_PATH)
    finally:
        app_no_auth.dependency_overrides.pop(require_principal, None)
        app_no_auth.dependency_overrides.pop(require_mcp_principal, None)
    assert response.status_code == 200
