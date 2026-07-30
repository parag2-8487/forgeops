# SPDX-License-Identifier: FSL-1.1-ALv2
"""`AppTokenVerifier` and the deny-by-default dependencies (design.md §11.2, §14.1).

Every token in this module is signed at runtime with a key pair generated inside the
test session, so nothing resembling a credential is ever committed — a pre-baked signed
JWT in a fixture is indistinguishable from a leaked one to a secret scanner, and
indistinguishable from a real one to an attacker if the key were ever reused.

The audience assertion is the load-bearing one. A token minted for the MCP gateway must
not verify against the product API: that is the enforceable half of RFC 9207's mix-up
defence at a resource server, and it is why the two verifiers are separate instances
rather than one shared one.
"""

from __future__ import annotations

import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.utils import base64url_encode
from src.auth.dependencies import require_principal, require_role, route_requires_principal
from src.auth.models import UserRole
from src.auth.public_routes import PUBLIC_PATHS, PUBLIC_ROUTES, is_public
from src.auth.verifier import AppTokenVerifier
from src.core.errors import ProblemException

pytestmark = pytest.mark.mandatory

ISSUER = "https://idp.example.invalid/application/o/forgeops"
APP_AUDIENCE = "forgeops-api"
GATEWAY_AUDIENCE = "forgeops-mcp-gateway"
KEY_ID = "test-only-not-a-real-key"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    """A throwaway RSA key pair, generated in-session.

    2048 bits rather than 4096: this key exists for the duration of one test module and
    key generation is the slowest thing in the file.
    """
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(signing_key: rsa.RSAPrivateKey) -> dict:
    numbers = signing_key.public_key().public_numbers()

    def _b64(value: int) -> str:
        length = (value.bit_length() + 7) // 8
        return base64url_encode(value.to_bytes(length, "big")).decode()

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": KEY_ID,
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }


def mint(
    signing_key: rsa.RSAPrivateKey,
    *,
    audience: str = APP_AUDIENCE,
    issuer: str = ISSUER,
    role: str | None = "developer",
    subject: str | None = None,
    expires_in: int = 300,
    extra: dict | None = None,
) -> str:
    now = int(time.time())
    claims: dict = {
        "iss": issuer,
        "aud": audience,
        "sub": subject if subject is not None else str(uuid.uuid4()),
        "iat": now,
        "exp": now + expires_in,
        "email": "dev@example.invalid",
    }
    if role is not None:
        claims["forgeops_role"] = role
    if extra:
        claims.update(extra)
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": KEY_ID})


class _StubJwkClient:
    """Serves the in-session JWKS without a network call.

    This substitutes a TRANSPORT, not a collaborator: the real `PyJWKClient` is a
    JWKS *fetcher*, and the verifier's signature check, audience check, issuer check and
    claim requirements all still execute against a genuinely signed token. Standing up
    an HTTPS server per test would prove nothing extra about the verifier.
    """

    def __init__(self, jwks: dict) -> None:
        self._jwks = jwks

    def get_signing_key_from_jwt(self, token: str):  # noqa: ANN202 - mirrors PyJWKClient
        from jwt import PyJWKSet

        header = jwt.get_unverified_header(token)
        key_set = PyJWKSet.from_dict(self._jwks)
        for key in key_set.keys:
            if key.key_id == header.get("kid"):
                return key
        raise jwt.PyJWKClientError(f"no key for kid {header.get('kid')!r}")


@pytest.fixture()
def verifier(jwks: dict) -> AppTokenVerifier:
    instance = AppTokenVerifier(issuer=ISSUER, audience=APP_AUDIENCE)
    instance._jwks_clients[ISSUER] = _StubJwkClient(jwks)  # noqa: SLF001
    instance._jwks_cache_times[ISSUER] = time.time()  # noqa: SLF001
    return instance


class TestASoundTokenResolvesAPrincipal:
    async def test_the_subject_and_role_come_from_the_token(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        subject = str(uuid.uuid4())
        token = mint(signing_key, subject=subject, role="admin")
        principal = await verifier.verify_principal(f"Bearer {token}")
        assert principal.subject == subject
        assert principal.role is UserRole.ADMIN
        assert principal.kind == "user"

    async def test_the_blast_radius_is_derived_not_read(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """A token claiming `infrastructure` for a viewer must not get it (D-39)."""
        token = mint(
            signing_key,
            role="viewer",
            extra={"blast_radius": "infrastructure", "forgeops_blast_radius": "infrastructure"},
        )
        principal = await verifier.verify_principal(f"Bearer {token}")
        assert principal.blast_radius == "read_only"

    async def test_an_explicit_user_id_claim_is_preferred(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        user_id = uuid.uuid4()
        token = mint(signing_key, subject="not-a-uuid", extra={"forgeops_user_id": str(user_id)})
        principal = await verifier.verify_principal(f"Bearer {token}")
        assert principal.user_id == user_id

    async def test_a_uuid_subject_is_accepted_as_the_user_id(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        subject = uuid.uuid4()
        token = mint(signing_key, subject=str(subject))
        principal = await verifier.verify_principal(f"Bearer {token}")
        assert principal.user_id == subject


class TestEveryRejectionIsTheSameProblem:
    """Telling an unauthenticated caller which check failed tells them what to change,
    and the caller who benefits most from that is the one guessing."""

    async def test_a_missing_header_is_unauthenticated(self, verifier: AppTokenVerifier) -> None:
        with pytest.raises(ProblemException) as caught:
            await verifier.verify_principal(None)
        assert caught.value.problem.status == 401
        assert caught.value.problem.type.endswith("/unauthenticated")

    async def test_a_non_bearer_scheme_is_unauthenticated(self, verifier: AppTokenVerifier) -> None:
        with pytest.raises(ProblemException) as caught:
            await verifier.verify_principal("Basic bm90LWEtdG9rZW4=")
        assert caught.value.problem.type.endswith("/unauthenticated")

    async def test_a_gateway_audience_token_is_rejected(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """The RFC 9207 mix-up clause. A token minted for the MCP gateway must not
        verify against the product API."""
        token = mint(signing_key, audience=GATEWAY_AUDIENCE)
        with pytest.raises(ProblemException) as caught:
            await verifier.verify_principal(f"Bearer {token}")
        assert caught.value.problem.type.endswith("/unauthenticated")

    async def test_an_expired_token_is_rejected(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        token = mint(signing_key, expires_in=-60)
        with pytest.raises(ProblemException) as caught:
            await verifier.verify_principal(f"Bearer {token}")
        assert caught.value.problem.type.endswith("/unauthenticated")

    async def test_an_untrusted_issuer_is_rejected(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        token = mint(signing_key, issuer="https://evil.example.invalid/")
        with pytest.raises(ProblemException) as caught:
            await verifier.verify_principal(f"Bearer {token}")
        assert caught.value.problem.type.endswith("/unauthenticated")

    async def test_a_missing_role_claim_is_rejected_not_defaulted(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """Defaulting to `viewer` would make a malformed token a valid principal, and
        the audit log would then record a real actor for a request nobody made."""
        token = mint(signing_key, role=None)
        with pytest.raises(ProblemException) as caught:
            await verifier.verify_principal(f"Bearer {token}")
        assert caught.value.problem.type.endswith("/unauthenticated")

    async def test_an_unknown_role_is_rejected(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        token = mint(signing_key, role="superuser")
        with pytest.raises(ProblemException) as caught:
            await verifier.verify_principal(f"Bearer {token}")
        assert caught.value.problem.type.endswith("/unauthenticated")

    async def test_no_rejection_body_names_the_reason(
        self, verifier: AppTokenVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """Every 401 body must be indistinguishable, so the set of rendered bodies has
        exactly one member."""
        cases = [
            None,
            "Basic bm90LWEtdG9rZW4=",
            f"Bearer {mint(signing_key, expires_in=-60)}",
            f"Bearer {mint(signing_key, audience=GATEWAY_AUDIENCE)}",
            f"Bearer {mint(signing_key, issuer='https://evil.example.invalid/')}",
            f"Bearer {mint(signing_key, role='superuser')}",
            "Bearer not.a.jwt",
        ]
        bodies = set()
        for header in cases:
            with pytest.raises(ProblemException) as caught:
                await verifier.verify_principal(header)
            bodies.add(json.dumps(caught.value.problem.model_dump(exclude_none=True), sort_keys=True))
        assert len(bodies) == 1, bodies


class TestThePublicRouteSet:
    def test_it_has_exactly_the_seven_design_entries(self) -> None:
        """§4.4's table has seven rows; two of them name two paths each
        (`openapi.json`/`docs`, and the four auth endpoints), which is why the tuple is
        longer than the table."""
        assert len(PUBLIC_ROUTES) == 10
        assert len(PUBLIC_PATHS) == 10

    def test_every_entry_carries_a_reason(self) -> None:
        for route in PUBLIC_ROUTES:
            assert route.reason.strip(), route

    def test_the_pairing_exchange_is_public_and_says_why(self) -> None:
        entry = next(r for r in PUBLIC_ROUTES if r.path == "/api/v1/agents/pair/exchange")
        assert "no credential yet" in entry.reason
        assert "single-use" in entry.reason

    def test_a_public_path_is_public_only_for_its_declared_methods(self) -> None:
        assert is_public("/health", "GET")
        assert not is_public("/health", "POST")

    def test_matching_is_exact_not_prefix(self) -> None:
        """A prefix match would silently make every future route under a public prefix
        public too."""
        assert not is_public("/api/v1/auth/login/extra", "GET")
        assert not is_public("/health/ready/deep", "GET")

    def test_an_unlisted_route_requires_a_principal(self) -> None:
        assert route_requires_principal("/api/v1/projects", {"GET"})

    def test_a_listed_route_does_not(self) -> None:
        assert not route_requires_principal("/health", {"GET", "HEAD"})

    def test_a_mixed_method_route_still_requires_a_principal(self) -> None:
        """`/health` is public for GET only, so a route serving GET and DELETE on that
        path is not fully public and must carry the dependency."""
        assert route_requires_principal("/health", {"GET", "DELETE"})


class TestRequireRole:
    def test_no_roles_is_a_programming_error(self) -> None:
        """`require_role()` with no roles would admit nobody, which is never the
        intent — and as a silent no-op it would read like an authorisation check."""
        with pytest.raises(ValueError, match="admit nobody"):
            require_role()

    def test_it_returns_a_named_closure_the_checker_can_see(self) -> None:
        dependency = require_role(UserRole.ADMIN)
        assert "require_role" in dependency.__qualname__


class TestRequirePrincipalNeedsAComposedVerifier:
    async def test_a_missing_verifier_is_not_a_401(self) -> None:
        """A composition error must not look like a wall of correctly-rejected
        clients (D-23)."""
        from starlette.datastructures import Headers

        class _App:
            class state:  # noqa: N801 - mimics Starlette's attribute bag
                pass

        class _Request:
            app = _App()
            headers = Headers({})

            class state:  # noqa: N801
                pass

        with pytest.raises(RuntimeError, match="app_token_verifier"):
            await require_principal(_Request())  # type: ignore[arg-type]
