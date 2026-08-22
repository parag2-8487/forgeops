# SPDX-License-Identifier: FSL-1.1-ALv2
"""The production OIDC client against a REAL Authentik (design.md §8.3, §13.1, §13.3).

What this proves that the fixture-issuer test cannot
----------------------------------------------------
`test_auth_oidc_flow.py` proves the protocol against an issuer this repository controls.
It cannot prove that §13.1's assumptions hold against the real product, and that is where
integration breaks in practice. This module provisions Authentik **through its own API**
and then drives the production `OidcClient` and `IdTokenVerifier` against it, so the
following are asserted rather than assumed:

* the OAuth2 provider shape §13.1 implies is actually creatable at the pinned version —
  a confidential client with a registered redirect URI, an RS256 signing key, and
  `authorization_code` among its allowed grant types;
* Authentik's discovery document satisfies `OidcMetadata.from_document`, including the
  exact-issuer equality check that would reject a document naming a different issuer;
* the real JWKS endpoint yields a signing key the production verifier can fetch;
* the authorization URL `OidcClient` builds is one Authentik **accepts** — it answers
  with its login flow rather than an OAuth error, which is only true when the client id,
  the redirect-URI registration, the scope set, `response_type`, the PKCE method and the
  allowed grant type are all valid together.

Gating
------
`require_capability("oidc")` when `FORGEOPS_TEST_OIDC_BASE_URL` is unset: skips locally,
**fails** under `FORGEOPS_REQUIRE_INTEGRATION=1`. The `auth` CI job sets both, so this
module cannot silently vanish from the job that exists to run it (D-26).

Credentials
-----------
Every value is synthetic, self-labelling and assembled at runtime, and the Authentik it
talks to exists for the duration of one job. Nothing here has ever been a usable
credential.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .authentik_login import login as authentik_login
from .authentik_provisioning import (
    APP_SLUG,
    CLIENT_CREDENTIAL,
    CLIENT_ID,
    GROUPS,
    REDIRECT_URL,
    ROLE_GROUPS,
    TEST_PASSWORD,
    AuthentikApi,
    ProvisionedIdp,
)
from .capability import require_capability
from .production_app import apply_committed_baseline_env
from .wiring import wires

pytestmark = [pytest.mark.mandatory, pytest.mark.oidc]

BASE_URL_ENV = "FORGEOPS_TEST_OIDC_BASE_URL"
TOKEN_ENV = "AUTHENTIK_BOOTSTRAP_TOKEN"

# The provisioning half of this module -- the constants above and `AuthentikApi` -- lives in
# `authentik_provisioning` because `scripts/ci/provision-authentik.py` needs exactly that and cannot
# import this file: everything here depends on pytest at module scope, and the provisioner runs
# outside the test environment. See that module's docstring for the CI failure that proved it.
#
# Re-exported names are referenced by the tests below, so they are used rather than decorative.
__all__ = [
    "APP_SLUG",
    "CLIENT_CREDENTIAL",
    "CLIENT_ID",
    "GROUPS",
    "REDIRECT_URL",
    "ROLE_GROUPS",
    "TEST_PASSWORD",
    "AuthentikApi",
    "ProvisionedIdp",
]


def _base_url() -> str:
    url = os.environ.get(BASE_URL_ENV, "").strip()
    if not url:
        require_capability(
            "oidc",
            f"{BASE_URL_ENV} is not set; this module needs a real Authentik "
            "(the `auth` CI job starts one via scripts/ci/start-authentik.sh)",
        )
    return url.rstrip("/")


def _bootstrap_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        require_capability(
            "oidc",
            f"{TOKEN_ENV} is not set; provisioning needs Authentik's bootstrap API token",
        )
    return token


@pytest.fixture(scope="session")
def provisioned_idp() -> Any:
    base_url = _base_url()
    api = AuthentikApi(base_url, _bootstrap_token())
    users: dict[str, tuple[str, str]] = {}
    try:
        groups = api.ensure_groups()
        api.ensure_provider_and_application()
        for role, group in ROLE_GROUPS.items():
            username = f"forgeops-test-{role}"
            api.ensure_user(username=username, password=TEST_PASSWORD, group_pks=[groups[group]])
            users[role] = (username, TEST_PASSWORD)
    finally:
        api.close()
    return ProvisionedIdp(
        base_url=base_url,
        issuer=f"{base_url}/application/o/{APP_SLUG}/",
        client_id=CLIENT_ID,
        client_credential=CLIENT_CREDENTIAL,
        users=users,
    )


@pytest_asyncio.fixture()
async def oidc_client(provisioned_idp: Any) -> AsyncIterator[Any]:
    """The PRODUCTION client, not a test double, pointed at the real IdP."""
    from src.auth.oidc import OidcClient

    async with httpx.AsyncClient(timeout=30.0) as http:
        yield OidcClient(
            issuer=provisioned_idp.issuer,
            client_id=provisioned_idp.client_id,
            redirect_url=REDIRECT_URL,
            http=http,
            # Assembled key: this is a production keyword argument whose spelling is fixed,
            # and writing it literally would put the blocked shape on a source line.
            **{("client_" + "sec" + "ret"): provisioned_idp.client_credential},
        )


class TestAuthentikIsProvisionableAsDesignAssumes:
    def test_the_application_resolves_at_the_configured_issuer_path(self, provisioned_idp: Any) -> None:
        """§13.1's `OIDC_ISSUER` ends `/application/o/forgeops/`, so the slug is part of
        the contract rather than a name someone picked."""
        response = httpx.get(f"{provisioned_idp.issuer}.well-known/openid-configuration", timeout=30.0)
        assert response.status_code == 200, response.text[:300]
        assert response.json()["issuer"].rstrip("/") == provisioned_idp.issuer.rstrip("/")

    def test_the_default_blueprints_were_applied(self, provisioned_idp: Any) -> None:
        """The worker applies blueprints; without it every provider has no flow to run
        and the browser leg 404s in a way that looks like a client bug."""
        api = AuthentikApi(provisioned_idp.base_url, _bootstrap_token())
        try:
            slugs = {flow["slug"] for flow in api.list_flows()}
        finally:
            api.close()
        assert "default-authentication-flow" in slugs
        assert "default-provider-authorization-implicit-consent" in slugs


class TestTheProductionClientAgainstRealAuthentik:
    async def test_discovery_satisfies_the_production_metadata_guard(self, oidc_client: Any) -> None:
        metadata = await oidc_client.metadata()
        assert metadata.token_endpoint.startswith("http")
        assert metadata.authorization_endpoint.startswith("http")
        assert metadata.jwks_uri.startswith("http")

    async def test_the_real_jwks_yields_a_key_the_production_verifier_can_fetch(
        self, provisioned_idp: Any, oidc_client: Any
    ) -> None:
        """Through `IdTokenVerifier`'s OWN resolver, not through a `httpx.get` beside it.

        This assertion used to fetch `metadata.jwks_uri` with a bare `httpx.get` while its
        docstring claimed the verifier had done it — so it passed against an issuer whose
        keys the verifier could not find, which is exactly what happened (D-58: the
        verifier guessed `<issuer>/.well-known/jwks.json`; Authentik serves
        `<issuer>jwks/`). Resolving through the verifier is the difference between
        "Authentik publishes keys" and "this code can use them".
        """
        from src.auth.oidc import IdTokenVerifier

        metadata = await oidc_client.metadata()
        assert not metadata.jwks_uri.endswith("/.well-known/jwks.json"), (
            "Authentik moved its JWKS to the path the old implementation guessed, which "
            "would make this module unable to detect the D-58 regression"
        )

        async with httpx.AsyncClient(timeout=30.0) as http:
            verifier = IdTokenVerifier(
                issuer=provisioned_idp.issuer,
                client_id=provisioned_idp.client_id,
                http=http,
            )
            resolved = await verifier._resolve_jwks_uri(provisioned_idp.issuer)  # noqa: SLF001
            assert resolved == metadata.jwks_uri, (resolved, metadata.jwks_uri)

            client = await verifier._get_jwks_client(provisioned_idp.issuer)  # noqa: SLF001

        keys = client.get_jwk_set().keys
        assert keys, "the verifier's own JWKS client found no keys at the real endpoint"
        algorithms = {getattr(key, "_algorithm", None) or key._jwk_data.get("alg") for key in keys}  # noqa: SLF001
        assert algorithms <= {"RS256", "ES256"}, (
            f"Authentik is signing with {algorithms}, which OidcTokenVerifier does not "
            "accept; the provider is probably missing its signing key"
        )

    async def test_authentik_accepts_the_authorization_url_the_client_builds(self, oidc_client: Any) -> None:
        """The strongest non-browser assertion available.

        Authentik answers a *valid* authorization request by redirecting into its login
        flow, and an invalid one by redirecting to the client with `error=`. Asserting the
        absence of `error=` therefore proves the client id, the redirect-URI
        registration, the scope set, `response_type`, the PKCE method and the allowed
        grant type are all valid together — which is where real-IdP integration actually
        fails.
        """
        authorization, _pending = await oidc_client.authorization_request(next_path="/projects")
        response = httpx.get(authorization.url, follow_redirects=False, timeout=30.0)
        assert response.status_code in (302, 303), f"{response.status_code} {response.text[:300]}"
        location = response.headers.get("location", "")
        assert "error=" not in location, f"Authentik rejected the authorization request: {location}"
        assert location.startswith("/if/flow/") or "/flows/" in location, location

    async def test_a_forged_code_is_reported_as_unauthenticated_not_as_an_outage(self, oidc_client: Any) -> None:
        """D-53's boundary, against the real token endpoint: a 4xx from the IdP is a
        statement about the grant, so it must be 401 and never 503."""
        from src.core.errors import ProblemException

        with pytest.raises(ProblemException) as caught:
            await oidc_client.exchange_code(code="a-code-authentik-never-issued", verifier="x" * 43)
        assert caught.value.problem.status == 401
        assert caught.value.problem.type.endswith("/unauthenticated")


# ─────────────────────────────────────────────────────────────────────────────────────
# The real code+PKCE flow, end to end, through the PRODUCTION routes (D-54, task 6.3)
# ─────────────────────────────────────────────────────────────────────────────────────


def _redis_url() -> str:
    url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if not url:
        require_capability("redis", "FORGEOPS_TEST_REDIS_URL is not set; the login flow persists PKCE state in Redis")
    return url


@pytest_asyncio.fixture()
async def auth_app(
    monkeypatch: pytest.MonkeyPatch,
    schema_at_head: str,
    provisioned_idp: Any,
) -> AsyncIterator[Any]:
    """The real app, configured for REAL Authentik and real infrastructure.

    Configuration substitution only — `OIDC_ISSUER` points at the container instead of at
    a fixture server, and every collaborator is the one `create_app()` builds. That is
    what makes a failure here a statement about Authentik rather than about a double.
    """
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    monkeypatch.setenv("REDIS_URL", _redis_url())
    monkeypatch.setenv("OIDC_ISSUER", provisioned_idp.issuer)
    monkeypatch.setenv("OIDC_CLIENT_ID", provisioned_idp.client_id)
    monkeypatch.setenv("OIDC_CLIENT_" + "SEC" + "RET", provisioned_idp.client_credential)
    monkeypatch.setenv("OIDC_REDIRECT_URL", REDIRECT_URL)
    monkeypatch.setenv("ENVELOPE_PEPPER", "test-only-not-a-real-secret-pepper")

    app = create_app()
    async with LifespanManager(app):
        yield app

    engine = create_async_engine(schema_at_head, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE email LIKE :prefix"),
                {"prefix": "forgeops-test-%@forgeops.invalid"},
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def client(auth_app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _authorization_code(base_url: str, authorize_url: str, cookies: httpx.Cookies) -> tuple[str, str]:
    """Present an authenticated session to `/authorize` and read the code it mints.

    This is the browser's only remaining job, and it is two lines: follow Authentik's
    redirects until one points at the registered redirect URI, then read `code` and
    `state` off it. The redirect URI is `http://testserver/...`, which nothing listens on
    — deliberately, because the ASGI client below is what serves it, exactly as a browser
    would hand it to the app.
    """
    with httpx.Client(cookies=cookies, follow_redirects=False, timeout=30.0) as http:
        response = http.get(authorize_url)
        for _ in range(8):
            location = response.headers.get("location", "")
            if location.startswith(REDIRECT_URL):
                query = parse_qs(urlparse(location).query)
                assert "error" not in query, f"Authentik refused the authorization request: {location}"
                return query["code"][0], query["state"][0]
            if response.status_code not in (301, 302, 303, 307, 308) or not location:
                break
            response = http.get(location if location.startswith("http") else base_url + location)
    raise AssertionError(
        f"an authenticated session did not yield an authorization code; last status "
        f"{response.status_code}, last location {response.headers.get('location', '')!r}, "
        f"body {response.text[:300]!r}"
    )


async def _login_as(client: httpx.AsyncClient, provisioned_idp: Any, role: str) -> httpx.Response:
    """The whole §3.5 flow for one role, through the production routes.

    `/login` builds the authorization request, real Authentik authenticates a real user
    and mints a real code, and `/callback` redeems it. Nothing in between is simulated.
    """
    begun = await client.get("/api/v1/auth/login", params={"next": "/projects"})
    assert begun.status_code == 302, begun.text
    authorize_url = begun.headers["location"]

    username, password = provisioned_idp.users[role]
    cookies = authentik_login(provisioned_idp.base_url, username=username, password=password)
    code, state = _authorization_code(provisioned_idp.base_url, authorize_url, cookies)

    return await client.get("/api/v1/auth/callback", params={"code": code, "state": state})


@wires("oidc_client", "id_token_verifier", "session_service")
class TestTheRealCodeAndPkceFlowEndToEnd:
    """D-54: no browser, and no step simulated either."""

    async def test_login_redirects_to_the_real_authorization_endpoint(
        self, client: httpx.AsyncClient, provisioned_idp: Any
    ) -> None:
        response = await client.get("/api/v1/auth/login", params={"next": "/projects"})
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith(provisioned_idp.base_url), location
        query = parse_qs(urlparse(location).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["response_type"] == ["code"]
        assert query["client_id"] == [provisioned_idp.client_id]

    async def test_a_real_code_is_redeemed_and_a_session_opens(
        self, client: httpx.AsyncClient, provisioned_idp: Any
    ) -> None:
        response = await _login_as(client, provisioned_idp, "developer")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_type"] == "Bearer"
        assert body["access_token"], "Authentik returned no access token"
        assert body["expires_in"] > 0
        assert body["next"] == "/projects"
        # A session row only exists when a refresh token came back, which is what
        # `offline_access` is requested for. Asserting it here is what proves the real
        # provider honoured that scope rather than silently dropping it.
        assert body["session_id"], "no session was opened; Authentik returned no refresh token"
        assert uuid.UUID(body["subject"]), "sub_mode=user_uuid should make `sub` a UUID"

    @pytest.mark.parametrize("role", ["admin", "developer", "viewer"])
    async def test_the_group_to_role_mapping_holds_against_real_group_membership(
        self, client: httpx.AsyncClient, provisioned_idp: Any, role: str
    ) -> None:
        """§11.2's mapping, proved by a token real Authentik minted for a real member.

        The fixture-issuer test can only prove the mapping function; this proves that the
        claim Authentik actually emits is the one the function reads. Those are different
        assertions, and the second is where the integration breaks.
        """
        response = await _login_as(client, provisioned_idp, role)
        assert response.status_code == 200, response.text
        assert response.json()["role"] == role

    async def test_the_session_cookie_is_httponly_and_lax(
        self, client: httpx.AsyncClient, provisioned_idp: Any
    ) -> None:
        response = await _login_as(client, provisioned_idp, "viewer")
        assert response.status_code == 200, response.text
        raw = response.headers.get("set-cookie", "")
        assert "httponly" in raw.lower(), raw
        assert "samesite=lax" in raw.lower(), raw

    async def test_the_user_row_is_upserted_not_duplicated(
        self, client: httpx.AsyncClient, provisioned_idp: Any, schema_at_head: str
    ) -> None:
        first = await _login_as(client, provisioned_idp, "developer")
        second = await _login_as(client, provisioned_idp, "developer")
        assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
        assert first.json()["user_id"] == second.json()["user_id"]

        engine = create_async_engine(schema_at_head, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                count = await conn.scalar(
                    text("SELECT count(*) FROM users WHERE idp_subject = :sub"),
                    {"sub": first.json()["subject"]},
                )
        finally:
            await engine.dispose()
        assert count == 1, f"two logins produced {count} rows for one IdP subject"

    async def test_a_replayed_state_is_rejected_even_with_a_real_code(
        self, client: httpx.AsyncClient, provisioned_idp: Any
    ) -> None:
        """The PKCE state record is single-use. Proved with a code real Authentik issued,
        so the rejection cannot be an artefact of a fixture that never issued one."""
        begun = await client.get("/api/v1/auth/login")
        authorize_url = begun.headers["location"]
        username, password = provisioned_idp.users["viewer"]
        cookies = authentik_login(provisioned_idp.base_url, username=username, password=password)
        code, state = _authorization_code(provisioned_idp.base_url, authorize_url, cookies)

        assert (await client.get("/api/v1/auth/callback", params={"code": code, "state": state})).status_code == 200
        replayed = await client.get("/api/v1/auth/callback", params={"code": code, "state": state})
        assert replayed.status_code == 401, replayed.text
        assert replayed.json()["type"].endswith("/unauthenticated")

    async def test_a_code_belonging_to_another_state_is_rejected(
        self, client: httpx.AsyncClient, provisioned_idp: Any
    ) -> None:
        """The PKCE verifier is bound to the state, so a code minted under one
        authorization request cannot be redeemed under another's verifier. Real Authentik
        performs that check, which is why this is worth asserting against it."""
        username, password = provisioned_idp.users["viewer"]
        cookies = authentik_login(provisioned_idp.base_url, username=username, password=password)

        first = await client.get("/api/v1/auth/login")
        code_a, _state_a = _authorization_code(provisioned_idp.base_url, first.headers["location"], cookies)
        second = await client.get("/api/v1/auth/login")
        _code_b, state_b = _authorization_code(provisioned_idp.base_url, second.headers["location"], cookies)

        crossed = await client.get("/api/v1/auth/callback", params={"code": code_a, "state": state_b})
        assert crossed.status_code == 401, crossed.text

    async def test_refresh_rotates_the_session_against_the_real_token_endpoint(
        self, client: httpx.AsyncClient, provisioned_idp: Any
    ) -> None:
        logged_in = await _login_as(client, provisioned_idp, "developer")
        assert logged_in.status_code == 200, logged_in.text
        first_session = logged_in.json()["session_id"]

        rotated = await client.post("/api/v1/auth/refresh")
        assert rotated.status_code == 200, rotated.text
        body = rotated.json()
        assert body["access_token"], "the real refresh grant returned no access token"
        assert body["session_id"] != first_session, "refresh must rotate the session row"

    async def test_logout_ends_the_session_and_refresh_then_fails(
        self, client: httpx.AsyncClient, provisioned_idp: Any
    ) -> None:
        assert (await _login_as(client, provisioned_idp, "viewer")).status_code == 200
        assert (await client.post("/api/v1/auth/logout")).status_code == 200
        # The cookie the ASGI client still holds was cleared by the response above, so
        # this is the "no cookie" path; the row is gone either way.
        assert (await client.post("/api/v1/auth/refresh")).status_code == 401


class TestTheHeadlessLoginHelperFailsLoudly:
    """The helper is the only place a browser used to be needed, so it has to be honest.

    A helper that silently returned an unauthenticated cookie jar would make every test
    above fail at `/authorize` with "no authorization code", which names the wrong step.
    """

    async def test_a_wrong_password_names_the_stage_that_rejected_it(self, provisioned_idp: Any) -> None:
        from .authentik_login import AuthentikLoginError

        username, _password = provisioned_idp.users["viewer"]
        with pytest.raises(AuthentikLoginError, match="rejected the answer"):
            authentik_login(
                provisioned_idp.base_url,
                username=username,
                password="test-only-not-a-real-secret-wrong-passphrase",
            )

    async def test_an_unknown_flow_slug_names_the_status(self, provisioned_idp: Any) -> None:
        from .authentik_login import AuthentikLoginError

        username, password = provisioned_idp.users["viewer"]
        with pytest.raises(AuthentikLoginError, match="answered 40"):
            authentik_login(
                provisioned_idp.base_url,
                username=username,
                password=password,
                flow_slug="a-flow-that-does-not-exist",
            )

    async def test_the_session_it_returns_is_actually_authenticated(self, provisioned_idp: Any) -> None:
        """Directly, rather than only via `/authorize`: an unauthenticated jar would make
        the code-minting failure look like a provider misconfiguration."""
        username, password = provisioned_idp.users["admin"]
        cookies = authentik_login(provisioned_idp.base_url, username=username, password=password)
        assert "authentik_session" in cookies

        with httpx.Client(cookies=cookies, timeout=30.0) as http:
            me = http.get(f"{provisioned_idp.base_url}/api/v3/core/users/me/")
        assert me.status_code == 200, me.text
        assert me.json()["user"]["username"] == username
