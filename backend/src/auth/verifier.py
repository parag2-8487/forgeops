# SPDX-License-Identifier: FSL-1.1-ALv2
"""Product-API bearer verification (design.md §11.2, §14.1).

`AppTokenVerifier` extends the Phase 0 verifier rather than duplicating it: same JWKS
cache, same exact-issuer allowlist, same required claims and the same asymmetric-only
algorithm list. Two differences, both deliberate.

**The audience is distinct.** The product API's audience is not the MCP gateway's, so
a token minted for the gateway cannot be replayed against the product API or vice
versa. That is the enforceable half of RFC 9207's mix-up defence at a resource server,
and it is the reason the two verifiers are separate instances of one class rather than
one shared instance.

**Every failure reports the single `unauthenticated` problem type.** The gateway
distinguishes "expired" from "wrong audience" from "untrusted issuer", which is useful
for a machine client debugging its own configuration. The product API deliberately does
not: telling an unauthenticated caller which check failed tells them what to change,
and the caller who benefits most from that is the one guessing. The `WWW-Authenticate`
header still says `Bearer`, so a legitimate client knows what to present.
"""

from __future__ import annotations

import uuid

import httpx

from ..core.errors import ProblemException, problem
from ..core.security import Claims, OidcTokenVerifier
from .models import UserRole
from .principal import Principal


class AppTokenVerifier(OidcTokenVerifier):
    """Verifies product-API bearer tokens and resolves them to a `Principal`."""

    #: One 401 type for every failure mode. See the module docstring.
    problem_types = dict.fromkeys(  # noqa: RUF012 - overrides a class attribute by design
        (
            "missing",
            "scheme",
            "undecodable",
            "issuer",
            "expired",
            "audience",
            "signature",
        ),
        "unauthenticated",
    )

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_ttl_seconds: int = 600,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            allowed_issuers=[issuer] if issuer else [],
            audience=audience,
            jwks_ttl_seconds=jwks_ttl_seconds,
            http=http,
        )
        self._issuer = issuer

    def _reject(self, mode: str, title: str, detail: str) -> ProblemException:
        """Report through the registry, so the status cannot disagree with it.

        `detail` is dropped on purpose — see the module docstring. The argument is
        still accepted because the base class passes it, and refusing it would mean
        overriding `verify` too.
        """
        del title, detail
        return problem(self.problem_types[mode])

    async def verify_principal(self, authorization: str | None) -> Principal:
        """Verify a bearer token and resolve the caller.

        Claims are read defensively: a token that verifies but carries an unusable
        `sub` or an unrecognised role is rejected rather than resolved to a default.
        Defaulting an unknown role to `viewer` would look safe and would in fact make
        a malformed token a valid, if limited, principal — and the audit log would then
        record a real actor for a request nobody made.
        """
        claims: Claims = await self.verify(authorization)
        raw = claims.raw

        subject = claims.sub
        if not subject:
            raise problem("unauthenticated")

        role = self._role_from_claims(raw)
        user_id = self._uuid_claim(raw, "forgeops_user_id") or self._uuid_from_subject(subject)
        if user_id is None:
            raise problem("unauthenticated")

        return Principal.for_user(
            user_id=user_id,
            subject=subject,
            email=str(raw.get("email") or ""),
            role=role,
            tenant_id=self._uuid_claim(raw, "forgeops_tenant_id"),
            session_id=self._uuid_claim(raw, "sid"),
        )

    @staticmethod
    def _role_from_claims(raw: dict) -> UserRole:
        value = raw.get("forgeops_role")
        if not isinstance(value, str):
            raise problem("unauthenticated")
        try:
            return UserRole(value)
        except ValueError as exc:
            raise problem("unauthenticated") from exc

    @staticmethod
    def _uuid_claim(raw: dict, name: str) -> uuid.UUID | None:
        value = raw.get(name)
        if value in (None, ""):
            return None
        try:
            return uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            return None

    @staticmethod
    def _uuid_from_subject(subject: str) -> uuid.UUID | None:
        """A subject that is itself a UUID is accepted as the user id.

        Authentik's `sub` is a UUID, so the common case needs no extra claim. A
        non-UUID subject without a `forgeops_user_id` claim is rejected by the caller
        rather than hashed into one: a synthesised id would make two different
        subjects collide in the audit log if the hash ever changed.
        """
        try:
            return uuid.UUID(subject)
        except ValueError:
            return None
