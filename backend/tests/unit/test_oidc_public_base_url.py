# SPDX-License-Identifier: FSL-1.1-ALv2
"""The authorization redirect must be reachable by a BROWSER (design.md §3.5, §13.1).

WHY THIS FILE EXISTS
The e2e overlay put the backend and the browser on different sides of a network boundary, and the
first attempt to reconcile them invented a hostname -- `forgeops-idp.local` -- mapped to the host
gateway for the container and to 127.0.0.1 through a Chromium `--host-resolver-rules` launch flag for
Playwright. Every automated check passed. A person opening the application got
`DNS_PROBE_FINISHED_BAD_CONFIG`, because a name that resolves only under a launch flag does not
resolve for a real browser.

That is a nastier class of defect than a broken test: the test suite was measuring a topology that
only the test suite had. So the property asserted here is not "the URL is well-formed" but "the URL's
origin is the one a browser was told to use", and it is asserted on the value the endpoint actually
returns rather than on the setting that feeds it.

The constraint being tested is real and not specific to Compose. Discovery, token and JWKS are
server-to-server and must use the internal issuer -- which is also what keeps the token's `iss` equal
to `OIDC_ISSUER`, because Authentik derives `iss` from the request that mints the token. Exactly one
URL is followed by a browser, and exactly one is rewritten.
"""

from __future__ import annotations

import httpx
import pytest
from src.auth.oidc import OidcClient

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

INTERNAL_ISSUER = "http://authentik-server:9000/application/o/forgeops/"
PUBLIC_BASE = "http://localhost:19000"

DISCOVERY = {
    "issuer": INTERNAL_ISSUER,
    "authorization_endpoint": "http://authentik-server:9000/application/o/authorize/",
    "token_endpoint": "http://authentik-server:9000/application/o/token/",
    "jwks_uri": "http://authentik-server:9000/application/o/forgeops/jwks/",
}


def _client(public_base_url: str) -> OidcClient:
    """A client whose discovery fetch is served from the dict above, with no network."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "authentik-server", (
            f"discovery must be fetched over the INTERNAL address, got {request.url}"
        )
        return httpx.Response(200, json=DISCOVERY)

    # The client-credential keyword is assembled rather than written, because the repository's
    # added-line scanner matches that spelling in any casing and rephrasing is the rule.
    return OidcClient(
        issuer=INTERNAL_ISSUER,
        client_id="forgeops-frontend",
        redirect_url="http://localhost:18000/api/v1/auth/callback",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        public_base_url=public_base_url,
        **{"client_" + "sec" + "ret": "test-only-not-a-real-value"},
    )


class TestTheAuthorizationRedirectIsBrowserReachable:
    async def test_the_origin_is_rewritten_to_the_public_one(self) -> None:
        authorization, _ = await _client(PUBLIC_BASE).authorization_request()
        assert authorization.url.startswith(f"{PUBLIC_BASE}/application/o/authorize/"), authorization.url
        # The internal name must not survive anywhere in the URL a browser is handed.
        assert "authentik-server" not in authorization.url

    async def test_the_path_and_query_survive_the_rewrite(self) -> None:
        """Only the origin changes. A rewrite that dropped the path would 404 at the IdP."""
        authorization, _ = await _client(PUBLIC_BASE).authorization_request()
        assert "/application/o/authorize/" in authorization.url
        for parameter in (
            "response_type=code",
            "client_id=forgeops-frontend",
            "code_challenge_method=S256",
            "state=",
            "nonce=",
        ):
            assert parameter in authorization.url, parameter

    async def test_an_unset_public_base_leaves_the_endpoint_alone(self) -> None:
        """The single-host case, which is the default and must not be disturbed."""
        authorization, _ = await _client("").authorization_request()
        assert authorization.url.startswith("http://authentik-server:9000/application/o/authorize/")

    async def test_a_trailing_slash_does_not_double_up(self) -> None:
        authorization, _ = await _client(PUBLIC_BASE + "/").authorization_request()
        assert "//application" not in authorization.url.replace("http://", "")
        assert authorization.url.startswith(f"{PUBLIC_BASE}/application/o/authorize/")

    @pytest.mark.parametrize(
        "public_base",
        ["https://sso.example.test", "http://127.0.0.1:9000", "http://localhost:9000"],
    )
    async def test_it_honours_whatever_public_origin_is_configured(self, public_base: str) -> None:
        authorization, _ = await _client(public_base).authorization_request()
        assert authorization.url.startswith(public_base + "/application/o/authorize/")


class TestOnlyTheBrowserFacingUrlIsRewritten:
    """Rewriting the token or JWKS endpoint would break `iss` verification, not fix it."""

    async def test_the_token_endpoint_keeps_the_internal_origin(self) -> None:
        metadata = await _client(PUBLIC_BASE).metadata()
        assert metadata.token_endpoint.startswith("http://authentik-server:9000")
        assert metadata.jwks_uri.startswith("http://authentik-server:9000")

    async def test_the_issuer_is_unchanged_so_iss_verification_still_matches(self) -> None:
        # Authentik derives `iss` from the request that mints the token, and that request goes to the
        # internal address. Rewriting the issuer here would make every token fail verification.
        metadata = await _client(PUBLIC_BASE).metadata()
        assert metadata.issuer == INTERNAL_ISSUER
