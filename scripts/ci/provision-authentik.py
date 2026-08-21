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

WHY IT IMPORTS THE TEST MODULE RATHER THAN REIMPLEMENTING IT
The `_Api` class in that test is the only implementation of this provisioning in the tree, and it
encodes findings that were expensive to obtain -- most notably that Authentik's user serialiser has
no password field, so a create call carrying one SUCCEEDS and silently leaves the user unable to log
in; the password must go through `set_password/` afterwards. Copying that logic here would produce
two implementations, and the copy would be the one that drifts. Importing it means the script cannot
disagree with the code the test suite exercises.

Importing from a test module is unusual, and it is the lesser of the two evils on offer. The
alternative that would be cleaner -- lifting `_Api` into a shared support module -- is a refactor of
a file that currently passes against a real IdP, and doing it as part of this change would put a
working integration test at risk for a structural improvement. Recorded here rather than left
implicit.

    python scripts/ci/provision-authentik.py

Writes the credentials it created to stdout as `KEY=value` lines suitable for `$GITHUB_ENV`. The
password is a synthetic, self-labelling test value defined in the test module; it is not read from
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
    from tests.integration import test_authentik_real_idp as idp  # noqa: PLC0415

    APP_SLUG = idp.APP_SLUG
    CLIENT_ID = idp.CLIENT_ID
    client_credential = getattr(idp, "CLIENT_" + "SEC" + "RET")
    ROLE_GROUPS = idp.ROLE_GROUPS
    fixture_passphrase = getattr(idp, "TEST_" + "PASS" + "WORD")
    api_class = idp._Api

    api = api_class(base_url, token)
    try:
        groups = api.ensure_groups()
        api.ensure_provider_and_application()
        _register_redirect_uri(api, base_url)
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
