# SPDX-License-Identifier: FSL-1.1-ALv2
"""Cross-cutting bearer-verification contract (Design §5.2, §5.7).

Bearer verification is needed by more than one domain: the MCP Gateway uses it on
every `/api/v1/mcp*` request, and `/api/v1/ai/complete` uses it to obtain the
`sub` that keys the rate-limit bucket (§11.7.5). That makes it a *cross-cutting*
concern, so the contract lives in `core` rather than in either domain.

Without this, `src/ai` would have to import `src/mcp` purely to name a type,
which is exactly the domain-to-domain coupling the modular-monolith rule forbids
and which the Ruff `flake8-tidy-imports` ban enforces.

**Phase 1 moved the concrete `OidcTokenVerifier` here too**, and the reason is the
same rule one step further on. §11.2's `AppTokenVerifier` must *extend* it rather
than duplicate it — two copies of a token verifier is two places for a
verification bug to live — but `src/auth` importing `src/mcp` is precisely the
banned coupling. Since `core` already owned the contract, and a bearer verifier is
a cross-cutting primitive rather than an MCP concern, the implementation belongs
here. `src/mcp/auth.py` re-exports it so the gateway's imports are unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx
import jwt
from jwt import PyJWKClient

from .errors import ProblemException


@runtime_checkable
class VerifiedClaims(Protocol):
    """The subset of verified token claims that callers outside the gateway rely on.

    Only `sub` is required here: it is the stable subject identifier used as the
    rate-limit key. Anything richer (audience, issuer, scopes) is the verifying
    domain's business and is deliberately not part of this contract.
    """

    sub: str
    iss: str
    exp: int


@runtime_checkable
class TokenVerifier(Protocol):
    """Verifies a bearer token or raises an RFC 9457 `ProblemException`.

    Implementations MUST fail closed: any doubt about signature, issuer, audience
    or expiry is a rejection, never a pass-through with reduced claims.
    """

    async def verify(self, authorization: str | None) -> Any:
        """Return verified claims, or raise `ProblemException` with status 401."""
        ...


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

    The RFC 9457 suffixes are overridable so a subclass can report failures under its
    own problem types without re-implementing any verification. §11.2's
    `AppTokenVerifier` uses that: the app API reports every verification failure as
    the single registered `unauthenticated` type, because telling a caller *which*
    check failed is an oracle it does not need.
    """

    #: Problem-type suffix per failure mode. A subclass overrides the mapping, never
    #: the verification.
    problem_types: dict[str, str] = {  # noqa: RUF012 - intentionally class-level and overridable
        "missing": "mcp-missing-token",
        "scheme": "mcp-invalid-auth-scheme",
        "undecodable": "mcp-invalid-token",
        "issuer": "mcp-untrusted-issuer",
        "expired": "mcp-token-expired",
        "audience": "mcp-invalid-audience",
        "signature": "mcp-token-verification-failed",
    }

    #: How long to wait for the issuer's discovery document before falling back to the
    #: well-known JWKS path (D-58). Short on purpose: this runs inside token
    #: verification, and a slow IdP must not turn every authenticated request into a
    #: hung connection. The result is cached with the JWKS client for `jwks_ttl_seconds`.
    _discovery_timeout: float = 5.0

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

    def _reject(self, mode: str, title: str, detail: str) -> ProblemException:
        return ProblemException(
            status=401,
            type_suffix=self.problem_types[mode],
            title=title,
            detail=detail,
        )

    async def verify(self, authorization: str | None) -> Claims:
        """Verify a bearer token. Raises ProblemException on failure."""
        token = self._require_bearer(authorization)

        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.DecodeError as e:
            raise self._reject("undecodable", "Invalid token", "Token could not be decoded.") from e

        issuer = unverified.get("iss")
        if not issuer or issuer not in self._allowed_issuers:
            raise self._reject(
                "issuer",
                "Untrusted token issuer",
                "The token issuer is not in the configured allowlist.",
            )

        try:
            jwks_client = await self._get_jwks_client(issuer)
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
            raise self._reject("expired", "Token expired", "The token has expired.") from e
        except jwt.InvalidAudienceError as e:
            raise self._reject(
                "audience",
                "Invalid audience",
                "The token audience does not match this gateway.",
            ) from e
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as e:
            raise self._reject(
                "signature",
                "Token verification failed",
                "Token signature could not be verified.",
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
            raise self._reject("missing", "Missing authentication", "A Bearer token is required.")
        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise self._reject(
                "scheme",
                "Invalid authentication scheme",
                "Authorization header must use Bearer scheme.",
            )
        return parts[1]

    async def _get_jwks_client(self, issuer: str) -> PyJWKClient:
        now = time.time()
        if issuer in self._jwks_clients:
            if now - self._jwks_cache_times.get(issuer, 0) < self._jwks_ttl:
                return self._jwks_clients[issuer]

        jwks_url = await self._resolve_jwks_uri(issuer)
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=self._jwks_ttl)
        self._jwks_clients[issuer] = client
        self._jwks_cache_times[issuer] = now
        return client

    async def _resolve_jwks_uri(self, issuer: str) -> str:
        """The issuer's `jwks_uri`, taken from its discovery document (D-58).

        This used to be `f"{issuer}/.well-known/jwks.json"`, guessed rather than
        discovered. That guess is not part of any specification — OIDC Discovery
        standardises `/.well-known/openid-configuration` and requires it to *name*
        `jwks_uri`, and providers put the keys wherever they like. Real Authentik serves
        `<issuer>jwks/`, so every token it minted was rejected as `signature` — the JWKS
        fetch 404'd and `PyJWKClientError` reads exactly like a bad signature. The bug
        survived because the test fixture issuers were written to serve the guessed path,
        so the guess and the discovery document agreed by construction.

        The fallback is deliberate and narrow: an issuer that publishes no discovery
        document, or one whose document omits `jwks_uri`, keeps the historical behaviour.
        Phase 0's MCP gateway accepts tokens from upstream issuers it does not control,
        and at least one test issuer publishes a JWKS and nothing else; turning that into
        a hard failure would be a behaviour change to a shipped contract in order to fix
        a different bug. `test_jwks_discovery.py` asserts the discovered URI WINS when the
        two differ, which is the assertion whose absence let this through.
        """
        fallback = f"{issuer.rstrip('/')}/.well-known/jwks.json"
        document = f"{issuer.rstrip('/')}/.well-known/openid-configuration"

        try:
            if self._http is not None:
                response = await self._http.get(document, timeout=self._discovery_timeout)
            else:
                async with httpx.AsyncClient(timeout=self._discovery_timeout) as http:
                    response = await http.get(document)
            if response.status_code != 200:
                return fallback
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return fallback

        if not isinstance(payload, dict):
            return fallback

        # The exact-issuer guard, at the one place a substituted document could redirect
        # key discovery. A document that names a different issuer is not this issuer's
        # metadata, whatever it says about keys.
        declared = payload.get("issuer")
        if isinstance(declared, str) and declared.rstrip("/") != issuer.rstrip("/"):
            return fallback

        jwks_uri = payload.get("jwks_uri")
        if isinstance(jwks_uri, str) and jwks_uri.startswith(("http://", "https://")):
            return jwks_uri
        return fallback
