# SPDX-License-Identifier: FSL-1.1-ALv2
"""An IdP outage degrades login, not readiness (design.md §6.3, §4.4; task 6.3).

§6.3 is explicit: "Keep Authentik out of `/health/ready`: an IdP outage must degrade
login, not readiness of authenticated traffic." That is a real operational decision, and
getting it backwards is the kind of mistake that only shows up during an incident — an
orchestrator would pull every backend replica out of service because the IdP is
restarting, taking down traffic that never needed the IdP at all.

The assertion is behavioural, not structural. Postgres and Redis are real; only the OIDC
issuer points at a closed port. If the readiness handler ever grew an IdP probe, this
would go red, whereas reading the source for the string "authentik" would not survive the
probe being added under any other name.

The other half of the same decision — that login itself reports the outage
distinguishably, as D-53's `idp-unavailable` (503) rather than as a credential failure —
is asserted here too, because the two halves are only correct together: keeping the IdP
out of readiness is what makes the login-side 503 the caller's only signal.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager

from .capability import require_capability
from .cerbos_stub import cerbos_health_stub
from .opa_stub import opa_health_stub
from .production_app import apply_committed_baseline_env
from .wiring import wires

pytestmark = pytest.mark.mandatory

#: A port on the loopback interface nothing listens on. Pointing the issuer here is a
#: transport substitution, which §0.4.1 permits; nothing inside the app is replaced.
UNREACHABLE_ISSUER = "http://127.0.0.1:1/application/o/forgeops/"


def _redis_url() -> str:
    url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if not url:
        require_capability("redis", "FORGEOPS_TEST_REDIS_URL is not set; readiness needs a real Redis")
    return url


@pytest_asyncio.fixture()
async def app_with_unreachable_idp(
    monkeypatch: pytest.MonkeyPatch,
    schema_at_head: str,
) -> AsyncIterator[Any]:
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    monkeypatch.setenv("REDIS_URL", _redis_url())
    monkeypatch.setenv("OIDC_ISSUER", UNREACHABLE_ISSUER)
    monkeypatch.setenv("ENVELOPE_PEPPER", "test-only-not-a-real-secret-pepper")

    # Task 6.4 put Cerbos INTO readiness, which is the precise opposite of what this
    # module asserts about the IdP — so Cerbos has to be reachable here or the 200
    # below would be a 503 for a reason that has nothing to do with §6.3. A transport
    # substitution (§0.4.1): the production client and a real socket, with a stub
    # process answering only the health path. Task 9.2 added OPA to readiness for the
    # same reason Cerbos is there, so it needs the same treatment.
    with cerbos_health_stub() as cerbos_url, opa_health_stub() as opa_url:
        monkeypatch.setenv("CERBOS_URL", cerbos_url)
        monkeypatch.setenv("OPA_URL", opa_url)
        app = create_app()
        async with LifespanManager(app):
            yield app


@pytest_asyncio.fixture()
async def client(app_with_unreachable_idp: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_with_unreachable_idp)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@wires("cerbos")
class TestReadinessIgnoresTheIdentityProvider:
    async def test_readiness_is_ready_with_the_idp_unreachable(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health/ready")
        assert response.status_code == 200, response.text

    async def test_no_probed_dependency_is_the_identity_provider(self, client: httpx.AsyncClient) -> None:
        """Named explicitly so ADDING an IdP probe fails, not just a failing one.

        A test that only asserted the 200 would still pass if a future readiness handler
        probed the IdP and happened to tolerate its absence — and then the next person to
        make that probe blocking would take out readiness with no test objecting.

        Written as "postgres and redis are present, and nothing IdP-shaped is" rather than
        as an exact set, because task 6.4 legitimately ADDS Cerbos to readiness: an
        authorisation sidecar outage really does block authenticated traffic, which is the
        precise difference from an IdP outage. An exact set here would have forced 6.4 to
        edit this test, and editing a §6.3 assertion to land §6.4 is indistinguishable
        from weakening it.
        """
        checks = set((await client.get("/health/ready")).json()["checks"])
        assert {"postgres", "redis"} <= checks, checks
        assert not checks & {"authentik", "authentik-server", "idp", "oidc", "auth"}, checks

    async def test_the_authorization_sidecar_is_probed(self, client: httpx.AsyncClient) -> None:
        """The other side of the same line, and the reason this module can host the
        `@wires("cerbos")` declaration.

        §2.3 draws the distinction: an IdP outage degrades login only, so Authentik stays
        out; an authorisation-sidecar outage refuses every non-public request under
        deny-by-default, so a replica that cannot reach Cerbos should be drained. Asserting
        only the absence of the IdP would leave "Cerbos is probed at all" untested, and a
        probe that silently disappeared would take the operational guarantee with it.
        """
        checks = (await client.get("/health/ready")).json()["checks"]
        assert checks.get("cerbos") == "ok", checks

    async def test_the_policy_engine_is_probed(self, client: httpx.AsyncClient) -> None:
        """Task 9.2's half of the same line (§11.7).

        Cerbos decides who may ask; OPA decides whether the governance bundle permits it. A
        replica that cannot reach OPA denies every mutation at the chokepoint's stage 1, so it
        is serving refusals for the whole governed surface and should be drained — the same
        argument as Cerbos, one layer up, and the opposite of the IdP's.
        """
        checks = (await client.get("/health/ready")).json()["checks"]
        assert checks.get("opa") == "ok", checks

    async def test_liveness_is_unaffected(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/health")).status_code == 200


class TestLoginReportsTheOutageDistinguishably:
    async def test_login_answers_idp_unavailable_not_unauthenticated(self, client: httpx.AsyncClient) -> None:
        """D-53. 503 means retry with backoff and keep the session; 401 would tell the
        client to discard its credential and re-authenticate through the IdP that is down.
        """
        response = await client.get("/api/v1/auth/login")
        assert response.status_code == 503, response.text
        assert response.json()["type"].endswith("/idp-unavailable")

    async def test_the_detail_names_no_url_and_no_upstream_error(self, client: httpx.AsyncClient) -> None:
        detail = (await client.get("/api/v1/auth/login")).json().get("detail") or ""
        assert "127.0.0.1" not in detail
        assert "1" != detail
        assert "Connect" not in detail and "connection" not in detail.lower()

    async def test_logout_still_succeeds_while_the_idp_is_down(self, client: httpx.AsyncClient) -> None:
        """Logout touches no IdP, so an outage must not strand a session cookie in a
        browser that will keep presenting it."""
        assert (await client.post("/api/v1/auth/logout")).status_code == 200
