# SPDX-License-Identifier: FSL-1.1-ALv2
"""Cross-cutting bearer-verification contract (Design §5.2, §5.7).

Bearer verification is needed by more than one domain: the MCP Gateway uses it on
every `/api/v1/mcp*` request, and `/api/v1/ai/complete` uses it to obtain the
`sub` that keys the rate-limit bucket (§11.7.5). That makes it a *cross-cutting*
concern, so the contract lives in `core` rather than in either domain.

Without this, `src/ai` would have to import `src/mcp` purely to name a type,
which is exactly the domain-to-domain coupling the modular-monolith rule forbids
and which the Ruff `flake8-tidy-imports` ban enforces. The concrete
`OidcTokenVerifier` still lives in `src/mcp/auth.py`; it satisfies this Protocol
structurally, so nothing needs to import it to depend on it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


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
