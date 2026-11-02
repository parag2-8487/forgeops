# SPDX-License-Identifier: FSL-1.1-ALv2
"""A failed re-discovery keeps the JWKS client that worked instead of caching a guessed URL.

One slow discovery request replaced a working client with `/.well-known/jwks.json`, which Authentik
does not serve, for the whole TTL: every token was refused for ten minutes at a time (2026-11-02).
"""

from __future__ import annotations

import asyncio

from src.core.security import OidcTokenVerifier

ISSUER = "http://idp.test/application/o/app/"


class _Verifier(OidcTokenVerifier):
    problem_types = dict.fromkeys(  # noqa: RUF012
        ("missing", "scheme", "undecodable", "issuer", "expired", "audience", "signature"), "unauthenticated"
    )

    def __init__(self) -> None:
        super().__init__(allowed_issuers=[ISSUER], audience="app", jwks_ttl_seconds=600)
        self.discovered: str | None = ISSUER + "jwks/"

    async def _resolve_jwks_uri(self, issuer: str) -> str:
        return self.discovered or f"{issuer.rstrip('/')}/.well-known/jwks.json"


def test_a_failed_rediscovery_keeps_the_working_client() -> None:
    verifier = _Verifier()
    first = asyncio.run(verifier._get_jwks_client(ISSUER))
    assert first.uri == ISSUER + "jwks/"

    verifier._jwks_cache_times[ISSUER] -= 601  # the TTL has run out
    verifier.discovered = None  # and discovery fails this time
    again = asyncio.run(verifier._get_jwks_client(ISSUER))
    assert again is first, "a discovery hiccup must not replace a working JWKS client"

    verifier.discovered = ISSUER + "jwks/"
    verifier._jwks_cache_times[ISSUER] -= 31  # discovery is retried soon, not after ten minutes
    assert asyncio.run(verifier._get_jwks_client(ISSUER)) is not first
