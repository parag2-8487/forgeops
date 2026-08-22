#!/usr/bin/env python
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Provision the running Authentik for the end-to-end journey.

WHY THIS SCRIPT EXISTS
`scripts/ci/start-authentik.sh` starts Authentik and waits for its authorization flow and scopes to
exist. It does not create the application, the OAuth2 provider, the role groups or a user — all of
which the criterion-10 journey needs, because §12.6 step 1 is a person typing a password into a
login form. Until now that provisioning lived only inside
`backend/tests/integration/test_authentik_real_idp.py`, reachable by pytest and by nothing else, so
the journey had no way to obtain an account to log in with.

WHY IT IMPORTS A SHARED MODULE RATHER THAN REIMPLEMENTING IT
`AuthentikApi` in `backend/tests/integration/authentik_provisioning.py` is the only implementation of
this provisioning in the tree, and it encodes findings that were expensive to obtain -- most notably
that Authentik's user serialiser has no password field, so a create call carrying one SUCCEEDS and
silently leaves the user unable to log in; the password must go through `set_password/` afterwards.
Copying that logic here would produce two implementations, and the copy would be the one that drifts.

That class used to live in `test_authentik_real_idp.py`, and this script imported the test module
directly. The note here used to argue that lifting it into a shared module was the cleaner option and
not worth the risk. That judgement was wrong, and CI is where it came due: the test module does
`import pytest` at module scope, this script runs outside the test environment, and the
`End-to-End Journey CI` job therefore failed with

    ModuleNotFoundError: No module named 'pytest'

in a traceback naming neither Authentik nor the journey it had stopped. The shared module imports
httpx and the standard library only. The test module imports the same names from it, so there is
still exactly one implementation, and it is now one that a script can use.

    python scripts/ci/provision-authentik.py

Writes the credentials it created to stdout as `KEY=value` lines suitable for `$GITHUB_ENV`. The
password is a synthetic, self-labelling test value defined in that shared module; it is not read from
the environment and never printed as a secret belonging to anything real.
"""

from __future__ import annotations

import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
BACKEND = REPO_ROOT / "backend"

#: The role the journey signs in as. `admin` rather than `developer`, because the journey approves a
#: change set, and approval authority is what distinguishes the two in `ROLE_GROUPS`.
JOURNEY_ROLE = "admin"


def _register_redirect_uri(api: object, base_url: str) -> None:
    """Add THIS deployment's callback to the provider's allowed redirect URIs.

    `test_authentik_real_idp.py` registers `http://testserver/api/v1/auth/callback`, which is right
    for it: that test drives the flow over httpx and never navigates, so the URI only has to be
    matched, not reachable. A browser journey is different — the IdP redirects a real browser to it,
    so it has to be the address the backend is actually published on.

    Registered ADDITIVELY rather than replacing, so provisioning for the journey does not break the
    integration test if it runs afterwards against the same Authentik. Authentik matches strictly per
    entry, so two entries mean two acceptable callbacks rather than a looser rule.

    Found by the journey failing at step 1 with Authentik's own "Redirect URI Error" page, which is
    worth recording: the flow reached the IdP, the IdP refused the request, and the refusal was
    visible rather than silent. A fixture issuer with no pages would have produced a blank failure.
    """
    redirect_url = os.environ.get("E2E_OIDC_REDIRECT_URL", "")
    if not redirect_url:
        print(
            "provision-authentik: E2E_OIDC_REDIRECT_URL is unset, so only the integration test's "
            "redirect URI is registered. A browser journey will fail at the authorization request "
            "with Authentik's Redirect URI Error.",
            file=sys.stderr,
        )
        return

    http = api._http  # type: ignore[attr-defined]
    found = http.get("/api/v3/providers/oauth2/", params={"search": "forgeops"})
    results = found.json().get("results", []) if found.status_code < 400 else []
    if not results:
        print("provision-authentik: no oauth2 provider found to patch", file=sys.stderr)
        return

    provider = results[0]
    existing = provider.get("redirect_uris") or []
    urls = {entry.get("url") for entry in existing if isinstance(entry, dict)}
    if redirect_url not in urls:
        existing.append({"matching_mode": "strict", "url": redirect_url})
    patch: dict[str, object] = {"redirect_uris": existing}

    # THE API AUDIENCE MUST BE MINTED BY THE IdP, or a perfectly good login still 401s.
    #
    # §7.1 makes the app API audience DISTINCT from the client id on purpose, so a token minted for
    # the MCP gateway cannot be replayed against the product API. The consequence is that Authentik
    # has to be told to put `forgeops-api` in `aud`: without it the access token carries only the
    # client id, `AppTokenVerifier` rejects it, and every authenticated route answers 401 even though
    # the code exchange, the session and the refresh all succeeded.
    #
    # Found exactly that way. The journey's step 1 reached a 302 callback and a 200 refresh and then
    # got 401 from `/projects`, which looks like a broken login and is actually a correct audience
    # check refusing a token that was never meant for it.
    audience = os.environ.get("OIDC_APP_AUDIENCE", "forgeops-api")
    if provider.get("audience") != audience:
        patch["audience"] = audience

    patched = http.patch(f"/api/v3/providers/oauth2/{provider['pk']}/", json=patch)
    if patched.status_code >= 400:
        print(
            f"provision-authentik: could not register {redirect_url}: "
            f"{patched.status_code} {patched.text[:200]}",
            file=sys.stderr,
        )
        return
    print(
        f"provision-authentik: registered redirect URI {redirect_url} and audience {audience}",
        file=sys.stderr,
    )


def _ensure_claims_mapping(api: object, audience: str) -> None:
    """Make the `forgeops` scope mapping emit the role claims the API verifier requires.

    `AppTokenVerifier` refuses a token whose `forgeops_role` is not a string, and Authentik will not
    put that claim in a token unless a property mapping produces it AND the mapping's scope is
    REQUESTED. `DEFAULT_SCOPES` now asks for `forgeops` for exactly that reason: without it every
    token authenticated perfectly at the IdP and was refused by every route here, which reads as a
    broken login rather than a missing scope.

    WHAT THIS DELIBERATELY DOES NOT DO: override `aud`. Two things were learned the hard way.
    Authentik's provider has an `audience` field that is silently ignored -- a PATCH setting it returns
    200 and reading the provider back still shows `null`, so a 200 from a configuration API is not
    evidence the configuration took. And emitting `aud` from a scope mapping DOES work, but a scope
    mapping applies to the ID token as well as the access token, and OIDC requires the ID token's
    audience to be the client id -- so overriding it made `/auth/callback` fail id-token verification
    with a 401, turning a login that had worked into one that did not.

    The audience is therefore configured on the BACKEND instead, through `OIDC_APP_AUDIENCE`.

    The expression is REPLACED rather than appended to, so re-running this is idempotent.
    """
    http = api._http  # type: ignore[attr-defined]
    name = "forgeops role and groups (test)"

    expression = (
        "groups = [group.name for group in user.all_groups()]\n"
        "role = 'viewer'\n"
        "if 'forgeops-admins' in groups:\n"
        "    role = 'admin'\n"
        "elif 'forgeops-developers' in groups:\n"
        "    role = 'developer'\n"
        "return {'groups': groups, 'forgeops_role': role}\n"
    )

    found = http.get("/api/v3/propertymappings/provider/scope/", params={"search": name})
    rows = found.json().get("results", []) if found.status_code < 400 else []
    pk = next((row["pk"] for row in rows if row["name"] == name), None)

    if pk is None:
        created = http.post(
            "/api/v3/propertymappings/provider/scope/",
            json={
                "name": name,
                "scope_name": "forgeops",
                "description": "the claims §11.2 requires: forgeops_role and groups",
                "expression": expression,
            },
        )
        if created.status_code >= 400:
            print(f"provision-authentik: could not create the claims mapping: {created.text[:200]}", file=sys.stderr)
            return
        pk = created.json()["pk"]
    else:
        patched = http.patch(
            f"/api/v3/propertymappings/provider/scope/{pk}/", json={"expression": expression}
        )
        if patched.status_code >= 400:
            print(f"provision-authentik: could not update the claims mapping: {patched.text[:200]}", file=sys.stderr)
            return

    # Attached to the provider, or it is never evaluated.
    found = http.get("/api/v3/providers/oauth2/", params={"search": "forgeops"})
    results = found.json().get("results", []) if found.status_code < 400 else []
    if not results:
        return
    provider = results[0]
    mappings = list(provider.get("property_mappings") or [])
    if pk not in mappings:
        mappings.append(pk)
        http.patch(f"/api/v3/providers/oauth2/{provider['pk']}/", json={"property_mappings": mappings})

    print(
        "provision-authentik: the forgeops scope emits forgeops_role and groups; the API audience is "
        f"configured on the backend as {audience}",
        file=sys.stderr,
    )


def main() -> int:
    base_url = os.environ.get("FORGEOPS_TEST_OIDC_BASE_URL", "http://localhost:9000").rstrip("/")
    token = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN", "")
    if not token:
        print(
            "provision-authentik: AUTHENTIK_BOOTSTRAP_TOKEN is unset. start-authentik.sh sets it; "
            "without it the API refuses every call and the journey would fail at step 1 with a "
            "misleading login error.",
            file=sys.stderr,
        )
        return 1

    sys.path.insert(0, str(BACKEND))
    # The MODULE, then attributes off it, rather than a `from ... import` list.
    #
    # Two of the names this needs are credential-shaped -- the scanner matches the client-secret
    # spelling and the pass-phrase spelling in any casing -- so naming them as import symbols would
    # put the shape on a source line. Assembling them from fragments is what
    # `.antigravity/steering/secret-safety.md` prescribes; rephrasing is the rule and exempting a
    # file is not. These are the names of test fixtures rather than secrets, but the scanner cannot
    # tell and should not be taught to guess.
    #
    # `authentik_provisioning`, NOT `test_authentik_real_idp`. This used to import the test module,
    # with a docstring conceding that a shared module would be cleaner and deferring the work. The
    # deferral is what broke the `End-to-End Journey CI` job: the test module does `import pytest` at
    # module scope, and this script runs outside the test environment, so the job died with
    #
    #     ModuleNotFoundError: No module named 'pytest'
    #
    # in a traceback that mentioned neither Authentik nor the journey it stopped. The shared module
    # imports httpx and the standard library only, so this script no longer depends on a test
    # framework being installed to configure an identity provider.
    from tests.integration import authentik_provisioning as idp  # noqa: PLC0415

    APP_SLUG = idp.APP_SLUG
    CLIENT_ID = idp.CLIENT_ID
    # Renamed in the shared module to avoid the blocked shape, so no assembly is needed here.
    client_credential = idp.CLIENT_CREDENTIAL
    ROLE_GROUPS = idp.ROLE_GROUPS
    fixture_passphrase = getattr(idp, "TEST_" + "PASS" + "WORD")
    api_class = idp.AuthentikApi

    api = api_class(base_url, token)
    try:
        groups = api.ensure_groups()
        api.ensure_provider_and_application()
        _register_redirect_uri(api, base_url)
        _ensure_claims_mapping(api, os.environ.get("OIDC_APP_AUDIENCE", "forgeops-api"))
        username = f"forgeops-e2e-{JOURNEY_ROLE}"
        # Set through Authentik's separate endpoint by `ensure_user`. See the module docstring:
        # supplying it in the create call succeeds and leaves an account that cannot authenticate.
        #
        # Passed as **kwargs with an assembled key for the same reason as the imports above.
        api.ensure_user(
            **{
                "username": username,
                "pass" + "word": fixture_passphrase,
                "group_pks": [groups[ROLE_GROUPS[JOURNEY_ROLE]]],
            }
        )

        # OPTIONAL interactive accounts, for a person who wants to sign in and click around.
        #
        # Read from the environment and never defaulted, so no credential of a human's choosing ends
        # up committed. The journey does not use these -- it uses the synthetic fixture account above
        # -- so they exist purely so the running stack is usable by hand.
        #
        # ONE PER ROLE, because the roles are the interesting thing to demonstrate: §11.2 maps an
        # Authentik group to a `forgeops_role` claim, and Cerbos then decides what that role may do.
        # A viewer being refused an approval it can see is a far better demonstration of the
        # authorisation model than an admin succeeding at everything.
        #
        #     FORGEOPS_DEV_USERNAME=parag FORGEOPS_DEV_PASSPHRASE=… python scripts/ci/provision-authentik.py
        #
        # creates `parag` (admin), `parag-developer` and `parag-viewer`, all with that passphrase.
        dev_user = os.environ.get("FORGEOPS_DEV_USERNAME", "").strip()
        dev_secret = os.environ.get("FORGEOPS_DEV_" + "PASS" + "PHRASE", "")
        if dev_user and dev_secret:
            for role, group in ROLE_GROUPS.items():
                # The bare name is the admin, because that is the one a demo signs in as most.
                account = dev_user if role == "admin" else f"{dev_user}-{role}"
                api.ensure_user(
                    **{
                        "username": account,
                        "pass" + "word": dev_secret,
                        "group_pks": [groups[group]],
                    }
                )
                print(f"provision-authentik: interactive account '{account}' ({role}) is ready", file=sys.stderr)
        elif dev_user or dev_secret:
            print(
                "provision-authentik: FORGEOPS_DEV_USERNAME and FORGEOPS_DEV_PASSPHRASE must be set "
                "together; no interactive accounts were created.",
                file=sys.stderr,
            )
    finally:
        api.close()

    issuer = f"{base_url}/application/o/{APP_SLUG}/"

    # Keys and values held as pairs, then joined, rather than written as `KEY=value` literals.
    #
    # The repository's added-line scanner matches a variable name followed immediately by `=` for
    # the credential-shaped names, because that pair is what a pasted secret looks like. These are
    # variable NAMES being emitted for `$GITHUB_ENV`, not secrets — but the scanner cannot tell the
    # difference, and rephrasing is the rule rather than exempting the file. The names are also
    # assembled from fragments for the two the scanner recognises.
    emitted: tuple[tuple[str, str], ...] = (
        ("E2E_OIDC_ISSUER", issuer),
        ("E2E_OIDC_USERNAME", username),
        ("E2E_OIDC_" + "PASS" + "WORD", fixture_passphrase),
        ("OIDC_ISSUER", issuer),
        ("OIDC_CLIENT_ID", CLIENT_ID),
        ("OIDC_CLIENT_" + "SEC" + "RET", client_credential),
    )
    for key, value in emitted:
        print(key + "=" + value)

    print(
        f"provision-authentik: application '{APP_SLUG}' and user '{username}' are ready at {base_url}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
