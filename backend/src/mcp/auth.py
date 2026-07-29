# SPDX-License-Identifier: FSL-1.1-ALv2
"""OAuth 2.1 / OIDC bearer verification with strict issuer checking."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from ..core.errors import ProblemException


@dataclass(frozen=True)
class Claims:
    """Verified OIDC token claims."""

    sub: str
    iss: str
    aud: str | list[str]
    exp: int
    iat: int
    raw: dict[str, Any] = field(default_factory=dict)


class OidcTokenVerifier:
    """Verifies bearer tokens against configured OIDC issuers.

    - Exact issuer allowlist check
    - JWKS fetched per-issuer with TTL caching
    - Signature verification (RS256, ES256)
    - Required claims: exp, iat, iss, aud
    """

    def __init__(
        self,
        *,
        allowed_issuers: list[str],
        audience: str,
        jwks_ttl_seconds: int = 600,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._allowed_issuers = set(allowed_issuers)
        self._audience = audience
        self._jwks_ttl = jwks_ttl_seconds
        self._http = http
        self._jwks_clients: dict[str, PyJWKClient] = {}
        self._jwks_cache_times: dict[str, float] = {}

    async def verify(self, authorization: str | None) -> Claims:
        """Verify a bearer token. Raises ProblemException on failure."""
        token = self._require_bearer(authorization)

        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.DecodeError as e:
            raise ProblemException(
                status=401,
                type_suffix="mcp-invalid-token",
                title="Invalid token",
                detail="Token could not be decoded.",
            ) from e

        issuer = unverified.get("iss")
        if not issuer or issuer not in self._allowed_issuers:
            raise ProblemException(
                status=401,
                type_suffix="mcp-untrusted-issuer",
                title="Untrusted token issuer",
                detail="The token issuer is not in the configured allowlist.",
            )

        try:
            jwks_client = self._get_jwks_client(issuer)
            signing_key = jwks_client.get_signing_key_from_jwt(token)

            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self._audience,
                issuer=issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise ProblemException(
                status=401,
                type_suffix="mcp-token-expired",
                title="Token expired",
                detail="The token has expired.",
            ) from e
        except jwt.InvalidAudienceError as e:
            raise ProblemException(
                status=401,
                type_suffix="mcp-invalid-audience",
                title="Invalid audience",
                detail="The token audience does not match this gateway.",
            ) from e
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as e:
            raise ProblemException(
                status=401,
                type_suffix="mcp-token-verification-failed",
                title="Token verification failed",
                detail="Token signature could not be verified.",
            ) from e

        return Claims(
            sub=claims.get("sub", ""),
            iss=claims["iss"],
            aud=claims.get("aud", ""),
            exp=claims["exp"],
            iat=claims["iat"],
            raw=claims,
        )

    def _require_bearer(self, authorization: str | None) -> str:
        if not authorization:
            raise ProblemException(
                status=401,
                type_suffix="mcp-missing-token",
                title="Missing authentication",
                detail="A Bearer token is required.",
            )
        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise ProblemException(
                status=401,
                type_suffix="mcp-invalid-auth-scheme",
                title="Invalid authentication scheme",
                detail="Authorization header must use Bearer scheme.",
            )
        return parts[1]

    def _get_jwks_client(self, issuer: str) -> PyJWKClient:
        now = time.time()
        if issuer in self._jwks_clients:
            if now - self._jwks_cache_times.get(issuer, 0) < self._jwks_ttl:
                return self._jwks_clients[issuer]

        jwks_url = f"{issuer.rstrip('/')}/.well-known/jwks.json"
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=self._jwks_ttl)
        self._jwks_clients[issuer] = client
        self._jwks_cache_times[issuer] = now
        return client
