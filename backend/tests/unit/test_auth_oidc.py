# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit proofs for the OIDC flow's pure parts (design.md §3.5, §11.2; task 6.2).

Everything here is deterministic and needs no IdP: PKCE arithmetic, the group→role
mapping, the refresh-token MAC, the `next` validation and the discovery-document
guards. The flow itself is proven end to end against a real fixture issuer in
`tests/integration/test_auth_oidc_flow.py` — these tests exist so a failure in the
arithmetic is reported as arithmetic rather than as a login that mysteriously 401s.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from src.auth.models import UserRole
from src.auth.oidc import (
    DEFAULT_SCOPES,
    GROUP_ROLE_MAP,
    OidcMetadata,
    PendingLogin,
    PkceChallenge,
    TokenResponse,
    access_token_expiry,
    role_from_groups,
)
from src.auth.routes import _safe_next
from src.auth.sessions import HMAC_DIGEST_SIZE, refresh_token_hmac
from src.core.errors import PROBLEM_REGISTRY, ProblemException

from tests.synthetic_secrets import SYNTHETIC_MARKER


class TestPkce:
    """RFC 7636 §4.1–4.2."""

    def test_the_challenge_is_the_s256_of_the_verifier(self) -> None:
        pkce = PkceChallenge.generate()
        expected = base64.urlsafe_b64encode(hashlib.sha256(pkce.verifier.encode("ascii")).digest())
        assert pkce.challenge == expected.decode("ascii").rstrip("=")

    def test_the_method_is_s256_and_there_is_no_way_to_ask_for_plain(self) -> None:
        assert PkceChallenge.generate().method == "S256"
        # No `method` parameter exists on `generate`, so `plain` is unreachable rather
        # than merely discouraged. A switch would be one an attacker-influenced request
        # could try to flip.
        with pytest.raises(TypeError):
            PkceChallenge.generate(method="plain")  # type: ignore[call-arg]

    def test_the_verifier_meets_the_rfc_length_floor(self) -> None:
        # RFC 7636 §4.1: 43 characters minimum, 128 maximum.
        verifier = PkceChallenge.generate().verifier
        assert 43 <= len(verifier) <= 128

    def test_two_generations_do_not_collide(self) -> None:
        assert PkceChallenge.generate().verifier != PkceChallenge.generate().verifier

    def test_the_verifier_is_base64url_without_padding(self) -> None:
        verifier = PkceChallenge.generate().verifier
        assert "=" not in verifier
        assert "+" not in verifier
        assert "/" not in verifier


class TestGroupToRoleMapping:
    """§11.2: groups map to EXACTLY one of the three roles."""

    @pytest.mark.parametrize(
        ("groups", "expected"),
        [
            (["forgeops-admins"], UserRole.ADMIN),
            (["forgeops-developers"], UserRole.DEVELOPER),
            (["forgeops-viewers"], UserRole.VIEWER),
            (["admin"], UserRole.ADMIN),
            (["developer"], UserRole.DEVELOPER),
            (["viewer"], UserRole.VIEWER),
            (["FORGEOPS-ADMINS"], UserRole.ADMIN),
            (["  developer  "], UserRole.DEVELOPER),
        ],
    )
    def test_each_known_group_maps_to_its_role(self, groups: list[str], expected: UserRole) -> None:
        assert role_from_groups(groups) == expected

    @pytest.mark.parametrize(
        "groups",
        [None, [], ["unrecognised"], ["nothing", "at", "all"], 42, {"a": 1}, object()],
    )
    def test_anything_unrecognised_becomes_a_viewer(self, groups: object) -> None:
        """The narrowest role, not an exception and not a developer.

        A misconfigured group mapping must degrade to read-only, never to write access
        the IdP never asserted, and never to a login failure that would lock every user
        out of a working IdP.
        """
        assert role_from_groups(groups) == UserRole.VIEWER

    def test_the_wider_role_wins_when_a_user_is_in_two_groups(self) -> None:
        assert role_from_groups(["forgeops-viewers", "forgeops-admins"]) == UserRole.ADMIN
        assert role_from_groups(["forgeops-developers", "forgeops-viewers"]) == UserRole.DEVELOPER

    def test_a_bare_string_claim_is_accepted_as_one_group(self) -> None:
        assert role_from_groups("forgeops-admins") == UserRole.ADMIN

    def test_the_map_only_ever_yields_the_three_roles(self) -> None:
        assert set(GROUP_ROLE_MAP.values()) == set(UserRole)


class TestRefreshTokenMac:
    """§6.2: the column holds an HMAC, never the token."""

    def test_the_digest_is_32_bytes(self) -> None:
        digest = refresh_token_hmac("pepper-" + SYNTHETIC_MARKER, "token-" + SYNTHETIC_MARKER)
        assert len(digest) == HMAC_DIGEST_SIZE == 32

    def test_the_digest_does_not_contain_the_token(self) -> None:
        token = "token-" + SYNTHETIC_MARKER
        assert token.encode() not in refresh_token_hmac("pepper-" + SYNTHETIC_MARKER, token)

    def test_the_pepper_changes_the_digest(self) -> None:
        token = "token-" + SYNTHETIC_MARKER
        assert refresh_token_hmac("pepper-a", token) != refresh_token_hmac("pepper-b", token)

    def test_it_is_deterministic(self) -> None:
        args = ("pepper-" + SYNTHETIC_MARKER, "token-" + SYNTHETIC_MARKER)
        assert refresh_token_hmac(*args) == refresh_token_hmac(*args)

    def test_an_empty_pepper_is_refused_rather_than_silently_unkeyed(self) -> None:
        with pytest.raises(ValueError, match="ENVELOPE_PEPPER"):
            refresh_token_hmac("", "token-" + SYNTHETIC_MARKER)


class TestSafeNext:
    """`/login?next=` must not become an open redirect."""

    @pytest.mark.parametrize("value", ["/", "/projects", "/projects/1?tab=diff", "/a:b"])
    def test_same_origin_paths_survive(self, value: str) -> None:
        assert _safe_next(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "//evil.example",
            "https://evil.example",
            "http://evil.example",
            r"/\evil.example",
            r"\\evil.example",
            "javascript:alert(1)",
            "projects",
        ],
    )
    def test_everything_else_falls_back_to_root(self, value: str | None) -> None:
        assert _safe_next(value) == "/"


class TestDiscoveryDocumentGuards:
    """A discovery document is configuration arriving over the network, so it is
    validated rather than trusted."""

    def _document(self, **overrides: object) -> dict[str, object]:
        document: dict[str, object] = {
            "issuer": "https://idp.invalid/app/o/forgeops/",
            "authorization_endpoint": "https://idp.invalid/app/o/authorize/",
            "token_endpoint": "https://idp.invalid/app/o/token/",
            "jwks_uri": "https://idp.invalid/app/o/jwks/",
        }
        document.update(overrides)
        return document

    def test_a_matching_document_is_accepted(self) -> None:
        metadata = OidcMetadata.from_document("https://idp.invalid/app/o/forgeops/", self._document())
        assert metadata.token_endpoint.endswith("/token/")

    def test_a_trailing_slash_difference_is_not_a_mismatch(self) -> None:
        metadata = OidcMetadata.from_document("https://idp.invalid/app/o/forgeops", self._document())
        assert metadata.jwks_uri.endswith("/jwks/")

    def test_a_document_claiming_another_issuer_is_refused(self) -> None:
        with pytest.raises(ProblemException) as caught:
            OidcMetadata.from_document(
                "https://idp.invalid/app/o/forgeops/",
                self._document(issuer="https://evil.example/"),
            )
        assert caught.value.problem.status == 503
        assert caught.value.problem.type.endswith("/idp-unavailable")

    @pytest.mark.parametrize("missing", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
    def test_a_document_missing_an_endpoint_is_refused(self, missing: str) -> None:
        document = self._document()
        del document[missing]
        with pytest.raises(ProblemException) as caught:
            OidcMetadata.from_document("https://idp.invalid/app/o/forgeops/", document)
        assert caught.value.problem.status == 503

    def test_the_problem_type_is_registered_with_that_status(self) -> None:
        """D-53. The type exists in the registry, so the status cannot be invented at
        the raise site."""
        assert PROBLEM_REGISTRY["idp-unavailable"].status == 503


class TestTokenResponseParsing:
    def test_a_complete_payload_parses(self) -> None:
        parsed = TokenResponse.from_payload(
            {
                "access_token": "access-" + SYNTHETIC_MARKER,
                "id_token": "id-" + SYNTHETIC_MARKER,
                "refresh_token": "refresh-" + SYNTHETIC_MARKER,
                "expires_in": 300,
                "token_type": "bearer",
            }
        )
        assert parsed.refresh_token == "refresh-" + SYNTHETIC_MARKER
        assert parsed.expires_in == 300

    @pytest.mark.parametrize("missing", ["access_token", "id_token"])
    def test_a_payload_missing_either_token_is_unauthenticated(self, missing: str) -> None:
        payload = {"access_token": "a", "id_token": "b"}
        del payload[missing]
        with pytest.raises(ProblemException) as caught:
            TokenResponse.from_payload(payload)
        assert caught.value.problem.status == 401

    def test_a_missing_refresh_token_is_none_not_an_error(self) -> None:
        parsed = TokenResponse.from_payload({"access_token": "a", "id_token": "b"})
        assert parsed.refresh_token is None

    def test_a_non_integer_expires_in_degrades_to_zero(self) -> None:
        parsed = TokenResponse.from_payload({"access_token": "a", "id_token": "b", "expires_in": "soon"})
        assert parsed.expires_in == 0


class TestPendingLoginRoundTrip:
    def test_it_round_trips(self) -> None:
        pending = PendingLogin(verifier="v", nonce="n", next_path="/projects")
        assert PendingLogin.from_json(pending.to_json()) == pending

    def test_a_missing_next_defaults_to_root(self) -> None:
        assert PendingLogin.from_json('{"verifier": "v", "nonce": "n"}').next_path == "/"


class TestAccessTokenExpiry:
    def test_an_unparseable_token_uses_the_fallback(self) -> None:
        assert access_token_expiry("not-a-jwt", 42) == 42

    def test_exp_minus_iat_is_preferred_over_the_hint(self) -> None:
        import jwt

        token = jwt.encode({"iat": 1000, "exp": 1600}, "unused-" + SYNTHETIC_MARKER, algorithm="HS256")
        assert access_token_expiry(token, 42) == 600


class TestScopes:
    def test_offline_access_is_requested_or_refresh_would_have_nothing_to_present(self) -> None:
        assert "offline_access" in DEFAULT_SCOPES
        assert DEFAULT_SCOPES[0] == "openid"
