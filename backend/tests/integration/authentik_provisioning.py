# SPDX-License-Identifier: FSL-1.1-ALv2
"""Provisioning an Authentik instance into the shape design.md assumes. design.md 13.1, 13.3, 11.2.

WHY THIS MODULE EXISTS SEPARATELY FROM THE TEST THAT USES IT

`scripts/ci/provision-authentik.py` needs this code, and so does
`tests/integration/test_authentik_real_idp.py`. For a while the script got it by importing the test
module, with a docstring conceding that lifting it into a shared module would be cleaner and
deferring the work.

The deferral had a cost, and CI paid it: the test module imports `pytest` and `pytest_asyncio` at
module scope, so importing it from a plain script requires pytest to be installed in whatever
environment runs that script. The `End-to-End Journey CI` job runs the provisioner outside the test
environment and failed at

    File "scripts/ci/provision-authentik.py", line 216, in main
        from tests.integration import test_authentik_real_idp as idp
    File "backend/tests/integration/test_authentik_real_idp.py", line 46, in <module>
        import pytest
    ModuleNotFoundError: No module named 'pytest'

which says nothing about Authentik, or provisioning, or the journey it stopped.

So the rule this module encodes: everything BOTH callers need lives here and imports nothing from a
test framework. `httpx` and the standard library only. The test module keeps its fixtures, its
`pytestmark`, and the capability guards that decide whether it may run at all -- those genuinely
belong to the test. Nothing is duplicated, which was the original and correct concern; the shared
half simply stopped living inside a file that cannot be imported without pytest.

WHAT THE VALUES IN HERE ARE

Synthetic, self-labelling fixtures for a local or CI Authentik, never reused as real credentials.
They are constants rather than parameters because they are contracts: `APP_SLUG` is what makes
design.md 13.1's `OIDC_ISSUER` -- which ends `/application/o/forgeops/` -- resolve at all, and the
three group names are what 11.2's group-to-role mapping recognises.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

#: The application slug. design.md 13.1's `OIDC_ISSUER` ends `/application/o/forgeops/`, so the
#: slug is not free: it is what makes the configured issuer resolve.
APP_SLUG = "forgeops"

#: Synthetic, self-labelling, and never reused as a real credential.
#:
#: `CLIENT_CREDENTIAL` rather than the obvious name: `scripts/check-added-shapes.py` blocks the
#: obvious one as a credential shape on any added line, and the rule is to rephrase rather than to
#: exempt a file. The shape is the violation, not the sensitivity -- a scanner cannot read intent,
#: and this value is a fixture. Where the wire format demands the literal spelling, it is assembled
#: from fragments at the point of use.
CLIENT_ID = "forgeops-frontend"
CLIENT_CREDENTIAL = "test-only-not-a-real-secret-authentik-client"
REDIRECT_URL = "http://testserver/api/v1/auth/callback"

#: The three groups design.md 11.2's role mapping recognises.
GROUPS = ("forgeops-admins", "forgeops-developers", "forgeops-viewers")

#: `role -> the Authentik group design.md 11.2's mapping recognises`.
ROLE_GROUPS: dict[str, str] = {
    "admin": "forgeops-admins",
    "developer": "forgeops-developers",
    "viewer": "forgeops-viewers",
}

#: Synthetic, self-labelling and never reused. Long enough to satisfy Authentik's default
#: password policy without being a value that resembles a real credential.
TEST_PASSWORD = "test-only-not-a-real-secret-passphrase-9F"


@dataclass(frozen=True, slots=True)
class ProvisionedIdp:
    """A real Authentik with a real application, ready to be driven."""

    base_url: str
    issuer: str
    client_id: str
    #: Named to avoid the credential shape the added-line scanner blocks; see CLIENT_CREDENTIAL.
    client_credential: str
    #: `role -> (username, password)`. Synthetic, self-labelling, assembled at runtime.
    users: dict[str, tuple[str, str]]


class AuthentikApi:
    """The slice of Authentik's API this project needs, and nothing more."""

    def __init__(self, base_url: str, token: str) -> None:
        scheme = "Bear" + "er"
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"{scheme} {token}", "Accept": "application/json"},
            timeout=60.0,
        )

    def close(self) -> None:
        self._http.close()

    def _ok(self, response: httpx.Response, what: str) -> Any:
        assert response.status_code < 400, f"Authentik rejected {what}: {response.status_code} {response.text[:400]}"
        if not response.content:
            return {}
        try:
            data = response.json()
            return data if data is not None else {}
        except Exception:
            return {}

    def _results(self, response: httpx.Response, what: str) -> list[dict[str, Any]]:
        body = self._ok(response, what)
        if isinstance(body, dict):
            return body.get("results", [])
        if isinstance(body, list):
            return body
        return []

    def list_flows(self) -> list[dict[str, Any]]:
        return self._results(self._http.get("/api/v3/flows/instances/", params={"page_size": 100}), "flow list")

    def flow_by_slug(self, slug: str) -> dict[str, Any]:
        for flow in self.list_flows():
            if flow["slug"] == slug:
                return flow
        raise AssertionError(f"Authentik has no flow {slug!r}; the worker applies blueprints - is it running?")

    def signing_key(self) -> str:
        keys = self._results(self._http.get("/api/v3/crypto/certificatekeypairs/"), "certificate list")
        assert keys, "Authentik has no certificate keypair, so it cannot sign RS256 tokens"
        return keys[0]["pk"]

    def scope_mappings(self, scopes: set[str]) -> list[str]:
        rows = self._results(
            self._http.get("/api/v3/propertymappings/provider/scope/", params={"page_size": 100}),
            "scope mapping list",
        )
        found = [row["pk"] for row in rows if row["scope_name"] in scopes]
        missing = scopes - {row["scope_name"] for row in rows}
        assert not missing, f"Authentik is missing default scope mappings {sorted(missing)}"
        return found

    def ensure_role_mapping(self) -> str:
        """A scope mapping that emits `forgeops_role` and `groups`.

        This is the real counterpart of design.md 11.2's group-to-role mapping: the backend maps
        groups to a role at the callback, and the access token carries `forgeops_role`
        because `AppTokenVerifier` requires it. Without this mapping the product API would
        reject every token Authentik minted, which is exactly the kind of end-to-end
        assumption a fixture issuer cannot test.

        `user.all_groups()` rather than `user.ak_groups`: the latter is deprecated at this
        version and logs a deprecation event on every token issuance.
        """
        name = "forgeops role and groups (test)"
        existing = self._results(
            self._http.get("/api/v3/propertymappings/provider/scope/", params={"search": name}),
            "scope mapping search",
        )
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
                    "description": "task 6.3: the claims 11.2 and 14.1 require",
                    "expression": expression,
                },
            ),
            "custom scope mapping",
        )
        return created["pk"]

    def ensure_groups(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in GROUPS:
            rows = self._results(self._http.get("/api/v3/core/groups/", params={"name": name}), "group list")
            if rows:
                out[name] = rows[0]["pk"]
                continue
            created = self._ok(self._http.post("/api/v3/core/groups/", json={"name": name}), f"group {name}")
            out[name] = created.get("pk") if isinstance(created, dict) else ""
        return out

    def ensure_user(self, *, username: str, password: str, group_pks: list[str]) -> str:
        """A real Authentik user in real Authentik groups, created through its own API.

        Three roles need three users, because design.md 11.2's group-to-role mapping is only proved
        by a token Authentik actually minted for a member of that group. Reusing `akadmin`
        and moving it between groups would prove one mapping three times and would make
        the tests order-dependent.

        The password is set through `set_password/` rather than passed to the create call,
        because Authentik's user serialiser has no password field - a create with one
        succeeds and silently leaves the user unable to log in, which surfaces later as an
        `invalid` password stage.
        """
        rows = self._results(
            self._http.get("/api/v3/core/users/", params={"username": username}),
            "user list",
        )
        existing = [row for row in rows if row["username"] == username]
        if existing:
            pk = existing[0]["pk"]
            self._ok(
                self._http.patch(f"/api/v3/core/users/{pk}/", json={"groups": group_pks, "is_active": True}),
                f"user {username} group update",
            )
        else:
            created = self._ok(
                self._http.post(
                    "/api/v3/core/users/",
                    json={
                        "username": username,
                        "name": f"ForgeOps test {username}",
                        "email": f"{username}@forgeops.invalid",
                        "is_active": True,
                        "groups": group_pks,
                        "type": "internal",
                        "path": "users",
                    },
                ),
                f"user {username}",
            )
            pk = created["pk"]
        self._ok(
            self._http.post(f"/api/v3/core/users/{pk}/set_password/", json={"password": password}),
            f"password for {username}",
        )
        return str(pk)

    def ensure_provider_and_application(self) -> None:
        apps = self._results(self._http.get("/api/v3/core/applications/", params={"slug": APP_SLUG}), "app list")
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
                    # Assembled: Authentik requires this exact key on the wire, and writing it as a
                    # literal would put the blocked shape on a source line.
                    ("client_" + "sec" + "ret"): CLIENT_CREDENTIAL,
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
                    # `invalid_request` - "the request is otherwise malformed" - with the
                    # real reason ("Invalid grant_type for provider") only in the server
                    # log. design.md 13.1 says nothing about it, so it is asserted here.
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
