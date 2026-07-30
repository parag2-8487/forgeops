# SPDX-License-Identifier: FSL-1.1-ALv2
"""JWKS location is DISCOVERED, not guessed (design.md §11.2, D-58).

The bug this file exists to prevent
-----------------------------------
`OidcTokenVerifier` used to build its JWKS URL as `f"{issuer}/.well-known/jwks.json"`.
That path is not in any specification: OIDC Discovery standardises
`/.well-known/openid-configuration` and requires it to *name* `jwks_uri`, and providers
publish keys wherever they like. Real Authentik serves `<issuer>jwks/`, so the fetch 404'd,
`PyJWKClientError` was mapped to the `signature` failure mode, and every token real
Authentik minted was rejected as if its signature were bad.

It survived Phase 0 and most of Phase 1 because **the test fixture issuers were written to
serve the guessed path**, so the guess and the discovery document agreed by construction —
a fixture shaped around the implementation rather than around the protocol. The decisive
assertion is therefore not "a JWKS can be fetched", it is "when the two disagree, the
DISCOVERED one is used". Every issuer here deliberately serves its keys somewhere the old
guess would never have looked.

Nothing is mocked. Each case runs a real HTTP server on loopback and the real
`OidcTokenVerifier` over a real socket — a transport substitution, which §0.4.1 permits.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from src.core.errors import ProblemException
from src.core.security import OidcTokenVerifier

pytestmark = pytest.mark.mandatory

AUDIENCE = "forgeops-test-api"
KID = "test-only-kid"
SUBJECT = "test-only-subject"

#: Where the issuers below publish their keys. Not the path the old implementation
#: guessed — that is the whole point. It is Authentik's real shape.
UNGUESSABLE_JWKS_PATH = "/application/o/forgeops/jwks/"

#: The path the old implementation guessed, named once so the assertions can say
#: "and this was never requested".
GUESSED_JWKS_PATH = "/.well-known/jwks.json"

DISCOVERY_PATH = "/.well-known/openid-configuration"


def _b64u_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _Issuer:
    """A minimal but real OIDC issuer: a discovery document, a JWKS, and a signing key."""

    def __init__(
        self,
        *,
        jwks_path: str = UNGUESSABLE_JWKS_PATH,
        publish_discovery: bool = True,
        declared_issuer: str | None = None,
        jwks_uri_override: str | None = None,
        omit_jwks_uri: bool = False,
    ) -> None:
        self.jwks_path = jwks_path
        self.publish_discovery = publish_discovery
        self.declared_issuer = declared_issuer
        self.jwks_uri_override = jwks_uri_override
        self.omit_jwks_uri = omit_jwks_uri
        self.hits: list[str] = []

        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._pem = self._key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def mint(self, *, audience: str = AUDIENCE, subject: str = SUBJECT) -> str:
        now = int(time.time())
        return pyjwt.encode(
            {"iss": self.base, "aud": audience, "sub": subject, "iat": now, "exp": now + 300},
            self._pem,
            algorithm="RS256",
            headers={"kid": KID},
        )

    def jwks(self) -> dict[str, Any]:
        numbers = self._key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": KID,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64u_uint(numbers.n),
                    "e": _b64u_uint(numbers.e),
                }
            ]
        }

    def document(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "issuer": self.declared_issuer or self.base,
            "authorization_endpoint": f"{self.base}/authorize",
            "token_endpoint": f"{self.base}/token",
        }
        if not self.omit_jwks_uri:
            payload["jwks_uri"] = self.jwks_uri_override or f"{self.base}{self.jwks_path}"
        return payload


def _make_handler(issuer_under_test: _Issuer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: dict[str, Any] | None = None) -> None:
            body = json.dumps(payload or {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            issuer_under_test.hits.append(self.path)
            if self.path == DISCOVERY_PATH:
                if not issuer_under_test.publish_discovery:
                    self._send(404)
                    return
                self._send(200, issuer_under_test.document())
                return
            if self.path == issuer_under_test.jwks_path:
                self._send(200, issuer_under_test.jwks())
                return
            self._send(404)

        def log_message(self, fmt: str, *args: object) -> None:
            """Silence the stdlib access log; pytest output is signal, not noise."""

    return Handler


@contextmanager
def issuer(**kwargs: Any) -> Iterator[_Issuer]:
    served = _Issuer(**kwargs)
    try:
        yield served
    finally:
        served.shutdown()


def _verifier(base: str) -> OidcTokenVerifier:
    return OidcTokenVerifier(allowed_issuers=[base], audience=AUDIENCE, jwks_ttl_seconds=600)


class TestTheDiscoveredJwksUriIsUsed:
    async def test_a_token_verifies_when_the_keys_are_not_at_the_guessed_path(self) -> None:
        """The regression test. Before D-58 this raised `mcp-token-verification-failed`,
        because the only path the verifier ever looked at returned 404."""
        with issuer() as served:
            claims = await _verifier(served.base).verify(f"Bearer {served.mint()}")
        assert claims.sub == SUBJECT
        assert claims.iss == served.base

    async def test_the_discovery_document_is_actually_fetched_and_the_guess_is_not(self) -> None:
        """Guards against the fix appearing to work for the wrong reason — a broadened
        fallback that happened to hit the right path would pass the test above."""
        with issuer() as served:
            await _verifier(served.base).verify(f"Bearer {served.mint()}")
            hits = list(served.hits)
        assert DISCOVERY_PATH in hits, hits
        assert UNGUESSABLE_JWKS_PATH in hits, hits
        assert GUESSED_JWKS_PATH not in hits, (
            f"the verifier still asked for the guessed path; discovery is not authoritative: {hits}"
        )

    async def test_the_resolution_is_cached_within_the_ttl(self) -> None:
        """Discovery runs inside token verification, so a fetch per request would put an
        IdP round trip on every authenticated call."""
        with issuer() as served:
            verifier = _verifier(served.base)
            await verifier.verify(f"Bearer {served.mint()}")
            await verifier.verify(f"Bearer {served.mint()}")
            documents = [path for path in served.hits if path == DISCOVERY_PATH]
        assert len(documents) == 1, f"discovery was fetched {len(documents)} times for two verifications"


class TestTheFallbackIsNarrow:
    async def test_an_issuer_publishing_only_a_jwks_still_works(self) -> None:
        """Phase 0's MCP gateway accepts tokens from upstream issuers it does not control,
        and one of its own test issuers publishes a JWKS and no discovery document. D-58
        keeps that working rather than breaking a shipped contract to fix a different bug.
        """
        with issuer(jwks_path=GUESSED_JWKS_PATH, publish_discovery=False) as served:
            claims = await _verifier(served.base).verify(f"Bearer {served.mint()}")
        assert claims.sub == SUBJECT

    async def test_a_document_omitting_jwks_uri_falls_back(self) -> None:
        with issuer(jwks_path=GUESSED_JWKS_PATH, omit_jwks_uri=True) as served:
            claims = await _verifier(served.base).verify(f"Bearer {served.mint()}")
        assert claims.sub == SUBJECT

    async def test_a_document_declaring_another_issuer_does_not_redirect_key_discovery(self) -> None:
        """The exact-issuer guard at the one place a substituted document could point key
        discovery elsewhere. The document names a different issuer, so its `jwks_uri` is
        ignored — and because the keys are NOT at the fallback path, the token is rejected
        rather than quietly verified against whatever that document offered."""
        with issuer(declared_issuer="http://another-issuer.invalid/") as served:
            with pytest.raises(ProblemException) as caught:
                await _verifier(served.base).verify(f"Bearer {served.mint()}")
        assert caught.value.problem.status == 401

    async def test_a_relative_jwks_uri_is_refused_rather_than_joined(self) -> None:
        """A `jwks_uri` that is not absolute is not a URL this verifier will fetch. Joining
        it against the issuer would be inventing a location again, which is the mistake."""
        with issuer(jwks_uri_override="/application/o/forgeops/jwks/") as served:
            with pytest.raises(ProblemException):
                await _verifier(served.base).verify(f"Bearer {served.mint()}")
            assert GUESSED_JWKS_PATH in served.hits, (
                "a non-absolute jwks_uri must fall back to the well-known path, not be joined"
            )


class TestTheGuessIsGoneFromTheSource:
    def test_no_hardcoded_jwks_path_is_used_as_the_primary_lookup(self) -> None:
        """A structural backstop for a behavioural fix.

        The behavioural tests above are the real evidence, but they all depend on the
        fallback still existing — so a future change that made the fallback primary again
        would keep them green. This asserts the resolver reads a document.
        """
        import inspect

        source = inspect.getsource(OidcTokenVerifier._resolve_jwks_uri)  # noqa: SLF001
        assert "openid-configuration" in source, source[:200]
        assert "jwks_uri" in source, source[:200]
