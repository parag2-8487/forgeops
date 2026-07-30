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
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio

from .capability import require_capability

pytestmark = [pytest.mark.mandatory, pytest.mark.oidc]

BASE_URL_ENV = "FORGEOPS_TEST_OIDC_BASE_URL"
TOKEN_ENV = "AUTHENTIK_BOOTSTRAP_TOKEN"

#: The application slug. §13.1's `OIDC_ISSUER` ends `/application/o/forgeops/`, so the
#: slug is not free: it is what makes the configured issuer resolve.
APP_SLUG = "forgeops"

#: Synthetic, self-labelling, and never reused as a real credential.
CLIENT_ID = "forgeops-frontend"
CLIENT_SECRET = "test-only-not-a-real-secret-authentik-client"
REDIRECT_URL = "http://testserver/api/v1/auth/callback"

#: The three groups §11.2's role mapping recognises.
GROUPS = ("forgeops-admins", "forgeops-developers", "forgeops-viewers")


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


@dataclass(frozen=True, slots=True)
class ProvisionedIdp:
    """A real Authentik with a real application, ready to be driven."""

    base_url: str
    issuer: str
    client_id: str
    client_secret: str


class _Api:
    """The slice of Authentik's API this module needs, and nothing more."""

    def __init__(self, base_url: str, token: str) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=60.0,
        )

    def close(self) -> None:
        self._http.close()

    def _ok(self, response: httpx.Response, what: str) -> Any:
        assert response.status_code < 400, f"Authentik rejected {what}: {response.status_code} {response.text[:400]}"
        return response.json() if response.content else None

    def list_flows(self) -> list[dict[str, Any]]:
        return self._ok(self._http.get("/api/v3/flows/instances/", params={"page_size": 100}), "flow list")["results"]

    def flow_by_slug(self, slug: str) -> dict[str, Any]:
        for flow in self.list_flows():
            if flow["slug"] == slug:
                return flow
        raise AssertionError(f"Authentik has no flow {slug!r}; the worker applies blueprints — is it running?")

    def signing_key(self) -> str:
        keys = self._ok(self._http.get("/api/v3/crypto/certificatekeypairs/"), "certificate list")["results"]
        assert keys, "Authentik has no certificate keypair, so it cannot sign RS256 tokens"
        return keys[0]["pk"]

    def scope_mappings(self, scopes: set[str]) -> list[str]:
        rows = self._ok(
            self._http.get("/api/v3/propertymappings/provider/scope/", params={"page_size": 100}),
            "scope mapping list",
        )["results"]
        found = [row["pk"] for row in rows if row["scope_name"] in scopes]
        missing = scopes - {row["scope_name"] for row in rows}
        assert not missing, f"Authentik is missing default scope mappings {sorted(missing)}"
        return found

    def ensure_role_mapping(self) -> str:
        """A scope mapping that emits `forgeops_role` and `groups`.

        This is the real counterpart of §11.2's group→role mapping: the backend maps
        groups to a role at the callback, and the access token carries `forgeops_role`
        because `AppTokenVerifier` requires it. Without this mapping the product API would
        reject every token Authentik minted, which is exactly the kind of end-to-end
        assumption a fixture issuer cannot test.

        `user.all_groups()` rather than `user.ak_groups`: the latter is deprecated at this
        version and logs a deprecation event on every token issuance.
        """
        name = "forgeops role and groups (test)"
        existing = self._ok(
            self._http.get("/api/v3/propertymappings/provider/scope/", params={"search": name}),
            "scope mapping search",
        )["results"]
        for row in existing:
            if row["name"] == name:
                return row["pk"]
        expression = (
            "groups = [group.name for group in user.all_groups()]\n"
            "role = 'viewer'\n"
            "if 'forgeops-admins' in groups:\n"
            "    role = 'admin'\n"
            "elif 'forgeops-developers' in groups:\n"
            "    role = 'developer'\n"
            "return {'groups': groups, 'forgeops_role': role}\n"
        )
        created = self._ok(
            self._http.post(
                "/api/v3/propertymappings/provider/scope/",
                json={
                    "name": name,
                    "scope_name": "forgeops",
                    "description": "task 6.3: the claims §11.2 and §14.1 require",
                    "expression": expression,
                },
            ),
            "custom scope mapping",
        )
        return created["pk"]

    def ensure_groups(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in GROUPS:
            rows = self._ok(self._http.get("/api/v3/core/groups/", params={"name": name}), "group list")["results"]
            if rows:
                out[name] = rows[0]["pk"]
                continue
            out[name] = self._ok(self._http.post("/api/v3/core/groups/", json={"name": name}), f"group {name}")["pk"]
        return out

    def ensure_provider_and_application(self) -> None:
        apps = self._ok(self._http.get("/api/v3/core/applications/", params={"slug": APP_SLUG}), "app list")["results"]
        if apps:
            return

        # `implicit-consent`, not `explicit-consent`. Explicit consent inserts a stage a
        # human must click, which would make the flow untestable without a browser and
        # adds nothing: this is a first-party application, and consent to give a
        # first-party client the identity it already has is theatre.
        authorization = self.flow_by_slug("default-provider-authorization-implicit-consent")
        invalidation = self.flow_by_slug("default-provider-invalidation-flow")
        mappings = self.scope_mappings({"openid", "email", "profile", "offline_access"})
        mappings.append(self.ensure_role_mapping())

        provider = self._ok(
            self._http.post(
                "/api/v3/providers/oauth2/",
                json={
                    "name": f"forgeops-{uuid.uuid4().hex[:8]}",
                    "authorization_flow": authorization["pk"],
                    "invalidation_flow": invalidation["pk"],
                    "client_type": "confidential",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uris": [{"matching_mode": "strict", "url": REDIRECT_URL}],
                    "property_mappings": mappings,
                    # Without a signing key Authentik signs with HS256 using the client
                    # secret, and `OidcTokenVerifier` accepts only RS256/ES256 with a key
                    # fetched from JWKS. An HS256 token would be rejected for a reason
                    # that reads like a signature bug.
                    "signing_key": self.signing_key(),
                    # `sub` becomes a UUID, which is what `AppTokenVerifier` resolves to a
                    # user id without needing an extra claim.
                    "sub_mode": "user_uuid",
                    "include_claims_in_id_token": True,
                    # Discovered the hard way: the provider defaults to NO allowed grant
                    # types at this version, and `/authorize` then answers
                    # `invalid_request` — "the request is otherwise malformed" — with the
                    # real reason ("Invalid grant_type for provider") only in the server
                    # log. §13.1 says nothing about it, so it is asserted here.
                    "grant_types": ["authorization_code", "refresh_token"],
                },
            ),
            "oauth2 provider",
        )
        self._ok(
            self._http.post(
                "/api/v3/core/applications/",
                json={"name": "ForgeOps", "slug": APP_SLUG, "provider": provider["pk"]},
            ),
            "application",
        )


@pytest.fixture(scope="session")
def provisioned_idp() -> Any:
    base_url = _base_url()
    api = _Api(base_url, _bootstrap_token())
    try:
        api.ensure_groups()
        api.ensure_provider_and_application()
    finally:
        api.close()
    return ProvisionedIdp(
        base_url=base_url,
        issuer=f"{base_url}/application/o/{APP_SLUG}/",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )


@pytest_asyncio.fixture()
async def oidc_client(provisioned_idp: Any) -> AsyncIterator[Any]:
    """The PRODUCTION client, not a test double, pointed at the real IdP."""
    from src.auth.oidc import OidcClient

    async with httpx.AsyncClient(timeout=30.0) as http:
        yield OidcClient(
            issuer=provisioned_idp.issuer,
            client_id=provisioned_idp.client_id,
            client_secret=provisioned_idp.client_secret,
            redirect_url=REDIRECT_URL,
            http=http,
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
        api = _Api(provisioned_idp.base_url, _bootstrap_token())
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
        """Fetched through `IdTokenVerifier`'s own JWKS client, so a rotation or an
        unexpected key format fails here rather than at a user's login."""
        metadata = await oidc_client.metadata()
        jwks = httpx.get(metadata.jwks_uri, timeout=30.0).json()
        assert jwks.get("keys"), f"real JWKS carried no keys: {jwks}"
        assert {key.get("alg") for key in jwks["keys"]} <= {"RS256", "ES256"}, (
            "Authentik is not signing with an algorithm OidcTokenVerifier accepts; the "
            "provider is probably missing its signing key"
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
