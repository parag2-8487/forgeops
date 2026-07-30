# SPDX-License-Identifier: FSL-1.1-ALv2
"""The OIDC code+PKCE flow end to end (design.md §3.5, §11.2; task 6.2).

What is real here, and what is substituted
------------------------------------------
Real: `create_app()` driven through its ASGI lifespan, the composed `OidcClient`,
`IdTokenVerifier`, `AppTokenVerifier` and `SessionService`, a real PostgreSQL at head,
and a real Redis holding the single-use PKCE record.

Substituted: exactly one thing — the identity provider, replaced by a **local fixture
HTTP server**. §0.4.1 permits a transport substitution and forbids a collaborator
substitution, and this is the former: the app talks to it over real HTTP through its own
`httpx` client, fetches real JWKS from it, and verifies real RS256 signatures. Nothing
inside the app is replaced, so a signature check that stopped happening would fail here.

The signing key is generated per test run
-----------------------------------------
`.kiro/steering/secret-safety.md` forbids a committed pre-baked signed token. The RSA
key pair is created in-process at session start, exists only in memory, and every token
these tests use is minted from it during the run. No value here has ever been a usable
credential, and the private key never reaches disk.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .capability import require_capability
from .production_app import apply_committed_baseline_env
from .wiring import wires

pytestmark = pytest.mark.mandatory

TEST_REDIS_URL_ENV = "FORGEOPS_TEST_REDIS_URL"

#: Every row these tests create carries this marker in `users.email`, so teardown can
#: remove exactly its own rows and nothing else. The schema is shared with the §6.5
#: revision proofs, which roll their writes back; this module commits through the real
#: app, so it cleans up explicitly.
#:
#: The marker is on the email rather than on `idp_subject` because `sub` has to be a
#: real UUID: Authentik's is, and `AppTokenVerifier` resolves a subject that is itself a
#: UUID to the user id without needing an extra claim. A prefixed subject would make
#: every access token this fixture mints unverifiable — a property of the fixture, not
#: of the code, and one that would have hidden a genuine failure.
EMAIL_MARKER = "fixture-issuer-"

CLIENT_ID = "forgeops-test-client"
APP_AUDIENCE = "forgeops-test-api"
#: Self-labelling per the steering rule. Never a real credential, and the fixture
#: issuer accepts any value — it exists to prove the secret is *sent*, not to guard
#: anything.
CLIENT_SECRET = "test-only-not-a-real-secret-client-secret"


def _b64u_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass
class IssuerScript:
    """What the fixture issuer will do on the next token exchange.

    Mutable on purpose: each test sets the deviation it wants (an expired token, the
    wrong audience) and the flow runs unchanged. This is the IdP's behaviour being
    varied, not the app's.
    """

    subject: str = ""
    email: str = "fixture@example.invalid"
    name: str = "Fixture User"
    groups: list[str] = field(default_factory=lambda: ["forgeops-developers"])
    nonce: str | None = None
    id_token_lifetime: int = 300
    id_token_audience: str | None = None
    access_token_audience: str | None = None
    access_token_role: str = "developer"
    issue_refresh_token: bool = True
    refresh_token_value: str = ""
    rotate_refresh_token: bool = True
    token_endpoint_status: int = 200
    last_form: dict[str, list[str]] = field(default_factory=dict)
    token_calls: int = 0


class FixtureIssuer:
    """A minimal OIDC provider on loopback: discovery, JWKS and a token endpoint."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "fixture-" + uuid.uuid4().hex[:12]
        self.script = IssuerScript()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def issuer(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # ── token minting ────────────────────────────────────────────────────────
    def _private_pem(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self.key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def mint(self, claims: dict[str, Any]) -> str:
        return jwt.encode(claims, self._private_pem(), algorithm="RS256", headers={"kid": self.kid})

    def jwks(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.kid,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64u_uint(numbers.n),
                    "e": _b64u_uint(numbers.e),
                }
            ]
        }

    def _id_token(self) -> str:
        now = datetime.now(UTC)
        script = self.script
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": script.subject,
            "aud": script.id_token_audience or CLIENT_ID,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=script.id_token_lifetime)).timestamp()),
            "email": script.email,
            "name": script.name,
            "groups": list(script.groups),
            "sid": "idp-session-" + uuid.uuid4().hex[:8],
        }
        if script.nonce is not None:
            claims["nonce"] = script.nonce
        return self.mint(claims)

    def _access_token(self) -> str:
        now = datetime.now(UTC)
        script = self.script
        return self.mint(
            {
                "iss": self.issuer,
                "sub": script.subject,
                "aud": script.access_token_audience or APP_AUDIENCE,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=300)).timestamp()),
                "email": script.email,
                "forgeops_role": script.access_token_role,
            }
        )

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        issuer_self = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # noqa: A002 - stdlib signature
                """Silence the default stderr access log; it would drown the test output."""

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - stdlib signature
                path = urlparse(self.path).path
                base = issuer_self.issuer
                if path == "/.well-known/openid-configuration":
                    self._json(
                        200,
                        {
                            "issuer": base,
                            "authorization_endpoint": f"{base}/authorize",
                            "token_endpoint": f"{base}/token",
                            "jwks_uri": f"{base}/.well-known/jwks.json",
                        },
                    )
                    return
                if path == "/.well-known/jwks.json":
                    self._json(200, issuer_self.jwks())
                    return
                self._json(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802 - stdlib signature
                path = urlparse(self.path).path
                if path != "/token":
                    self._json(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode()
                script = issuer_self.script
                script.last_form = parse_qs(raw)
                script.token_calls += 1

                if script.token_endpoint_status != 200:
                    self._json(script.token_endpoint_status, {"error": "invalid_grant"})
                    return

                payload: dict[str, Any] = {
                    "access_token": issuer_self._access_token(),
                    "id_token": issuer_self._id_token(),
                    "token_type": "bearer",
                    "expires_in": 300,
                }
                if script.issue_refresh_token:
                    if script.rotate_refresh_token or not script.refresh_token_value:
                        script.refresh_token_value = "refresh-test-only-not-a-real-secret-" + uuid.uuid4().hex
                    payload["refresh_token"] = script.refresh_token_value
                self._json(200, payload)

        return Handler


def _require_redis_url() -> str:
    url = os.environ.get(TEST_REDIS_URL_ENV, "").strip()
    if not url:
        require_capability(
            "redis",
            f"{TEST_REDIS_URL_ENV} is not set; the single-use PKCE record lives in a "
            "real Redis (design.md §11.2)",
        )
    return url


@pytest.fixture(scope="session")
def redis_url() -> str:
    return _require_redis_url()


@pytest.fixture(scope="session")
def fixture_issuer() -> Iterator[FixtureIssuer]:
    issuer = FixtureIssuer()
    try:
        yield issuer
    finally:
        issuer.shutdown()


@pytest_asyncio.fixture()
async def auth_app(
    monkeypatch: pytest.MonkeyPatch,
    schema_at_head: str,
    redis_url: str,
    fixture_issuer: FixtureIssuer,
) -> AsyncIterator[Any]:
    """The real app, pointed at the fixture issuer and the real infrastructure."""
    from src.main import create_app

    # A fresh script per test. `fixture_issuer` is session-scoped because generating an
    # RSA key and starting a server per test is wasteful, but leaving its script mutable
    # across tests made the "expect 401" cases pass for the WRONG reason: one test set
    # `id_token_audience` to a foreign value and every later test inherited it, so five
    # tests were rejecting a token nobody had asked them to reject. Resetting here is
    # what keeps each case's failure mode its own.
    fixture_issuer.script = IssuerScript()

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("OIDC_ISSUER", fixture_issuer.issuer)
    monkeypatch.setenv("OIDC_APP_AUDIENCE", APP_AUDIENCE)
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OIDC_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("OIDC_REDIRECT_URL", "http://testserver/api/v1/auth/callback")
    monkeypatch.setenv("ENVELOPE_PEPPER", "test-only-not-a-real-secret-pepper")

    app = create_app()
    async with LifespanManager(app):
        yield app

    engine = create_async_engine(schema_at_head, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE email LIKE :prefix"),
                {"prefix": f"{EMAIL_MARKER}%"},
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def client(auth_app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _new_subject(issuer: FixtureIssuer) -> str:
    """A fresh IdP subject, and the matching self-identifying email.

    A UUID string, because that is what Authentik's `sub` is and what
    `AppTokenVerifier` resolves to a user id without an extra claim. The teardown marker
    goes on the email instead.
    """
    subject = str(uuid.uuid4())
    issuer.script.email = f"{EMAIL_MARKER}{subject}@example.invalid"
    return subject


async def _begin_login(client: httpx.AsyncClient, next_path: str = "/projects") -> tuple[str, str]:
    """Drive `/login` and return `(state, nonce)` the way the IdP would see them."""
    response = await client.get("/api/v1/auth/login", params={"next": next_path})
    assert response.status_code == 302, response.text
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    return query["state"][0], query["nonce"][0]


@wires("oidc_client", "id_token_verifier", "session_service")
class TestSuccessfulExchange:
    async def test_the_full_flow_opens_a_session_and_returns_an_access_token(
        self, client: httpx.AsyncClient, auth_app: Any, fixture_issuer: FixtureIssuer
    ) -> None:
        subject = _new_subject(fixture_issuer)
        state, nonce = await _begin_login(client)
        fixture_issuer.script.subject = subject
        fixture_issuer.script.nonce = nonce

        response = await client.get("/api/v1/auth/callback", params={"code": "any-code", "state": state})
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["token_type"] == "Bearer"
        assert body["role"] == "developer"
        assert body["subject"] == subject
        assert body["next"] == "/projects"
        assert body["session_id"]

        # The PKCE verifier really was presented, and the client secret with it.
        form = fixture_issuer.script.last_form
        assert form["grant_type"] == ["authorization_code"]
        assert form["code_verifier"][0]
        assert form["client_secret"] == [CLIENT_SECRET]

        # The cookie is httpOnly, Lax and holds the refresh token, not the access token.
        cookie_header = response.headers["set-cookie"]
        assert "httponly" in cookie_header.lower()
        assert "samesite=lax" in cookie_header.lower()
        assert body["access_token"] not in cookie_header

    async def test_the_returned_access_token_verifies_against_the_app_audience(
        self, client: httpx.AsyncClient, auth_app: Any, fixture_issuer: FixtureIssuer
    ) -> None:
        """The token the callback hands back is one the product API will accept.

        Verified through the app's OWN `AppTokenVerifier` from the real composition,
        fetching real JWKS over real HTTP — so this fails if the audience contract
        between login and the product API ever diverges.
        """
        subject = _new_subject(fixture_issuer)
        state, nonce = await _begin_login(client)
        fixture_issuer.script.subject = subject
        fixture_issuer.script.nonce = nonce
        body = (await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})).json()

        principal = await auth_app.state.app_token_verifier.verify_principal(f"Bearer {body['access_token']}")
        assert principal.subject == subject
        assert principal.kind == "user"
        assert principal.blast_radius == "workspace"

    async def test_the_user_row_is_upserted_not_duplicated(
        self, client: httpx.AsyncClient, auth_app: Any, fixture_issuer: FixtureIssuer
    ) -> None:
        subject = _new_subject(fixture_issuer)
        for groups in (["forgeops-developers"], ["forgeops-admins"]):
            state, nonce = await _begin_login(client)
            fixture_issuer.script.subject = subject
            fixture_issuer.script.nonce = nonce
            fixture_issuer.script.groups = list(groups)
            response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
            assert response.status_code == 200, response.text

        sessionmaker = auth_app.state.sessionmaker
        async with sessionmaker() as session:
            count = await session.execute(
                text("SELECT count(*), max(role::text) FROM users WHERE idp_subject = :s"),
                {"s": subject},
            )
            rows, role = count.one()
        assert rows == 1, "a second login must update the row, not create a second one"
        assert role == "admin", "the IdP's current groups must win at every login"


class TestReplayedState:
    async def test_a_second_callback_with_the_same_state_is_rejected(
        self, client: httpx.AsyncClient, fixture_issuer: FixtureIssuer
    ) -> None:
        state, nonce = await _begin_login(client)
        fixture_issuer.script.subject = _new_subject(fixture_issuer)
        fixture_issuer.script.nonce = nonce

        first = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
        assert first.status_code == 200, first.text
        calls_after_first = fixture_issuer.script.token_calls

        second = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
        assert second.status_code == 401
        assert second.json()["type"].endswith("/unauthenticated")
        assert fixture_issuer.script.token_calls == calls_after_first, (
            "a replayed state must be refused BEFORE the token endpoint is called"
        )

    async def test_an_unknown_state_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": "never-issued"})
        assert response.status_code == 401

    async def test_a_callback_without_a_code_is_rejected(self, client: httpx.AsyncClient) -> None:
        state, _ = await _begin_login(client)
        response = await client.get("/api/v1/auth/callback", params={"state": state})
        assert response.status_code == 401


class TestRejectedIdTokens:
    """Each case must fail at ID-token verification, not earlier.

    Every test here asserts the token endpoint was reached. Without that, a rejection
    caused by a broken `state` record would look identical to a rejection caused by the
    verification these tests exist to prove — and the suite would stay green while
    verifying nothing, which is the §0.4 failure mode.
    """

    async def test_an_expired_id_token_is_rejected(
        self, client: httpx.AsyncClient, fixture_issuer: FixtureIssuer
    ) -> None:
        state, nonce = await _begin_login(client)
        fixture_issuer.script.subject = _new_subject(fixture_issuer)
        fixture_issuer.script.nonce = nonce
        fixture_issuer.script.id_token_lifetime = -60

        response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
        assert response.status_code == 401
        assert response.json()["type"].endswith("/unauthenticated")
        assert fixture_issuer.script.token_calls == 1

    async def test_an_id_token_for_another_audience_is_rejected(
        self, client: httpx.AsyncClient, fixture_issuer: FixtureIssuer
    ) -> None:
        state, nonce = await _begin_login(client)
        fixture_issuer.script.subject = _new_subject(fixture_issuer)
        fixture_issuer.script.nonce = nonce
        fixture_issuer.script.id_token_audience = "some-other-client"

        response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
        assert response.status_code == 401
        assert fixture_issuer.script.token_calls == 1

    async def test_a_mismatched_nonce_is_rejected(
        self, client: httpx.AsyncClient, fixture_issuer: FixtureIssuer
    ) -> None:
        state, _ = await _begin_login(client)
        fixture_issuer.script.subject = _new_subject(fixture_issuer)
        fixture_issuer.script.nonce = "a-nonce-this-login-never-issued"

        response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
        assert response.status_code == 401
        assert fixture_issuer.script.token_calls == 1

    async def test_a_token_endpoint_rejection_is_401_not_503(
        self, client: httpx.AsyncClient, fixture_issuer: FixtureIssuer
    ) -> None:
        """D-53: a 4xx from the IdP is a statement about the grant, not availability."""
        state, nonce = await _begin_login(client)
        fixture_issuer.script.subject = _new_subject(fixture_issuer)
        fixture_issuer.script.nonce = nonce
        fixture_issuer.script.token_endpoint_status = 400
        response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
        assert response.status_code == 401
        assert fixture_issuer.script.token_calls == 1


class TestRefreshRotation:
    async def _login(self, client: httpx.AsyncClient, fixture_issuer: FixtureIssuer) -> str:
        state, nonce = await _begin_login(client)
        fixture_issuer.script.subject = _new_subject(fixture_issuer)
        fixture_issuer.script.nonce = nonce
        response = await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})
        assert response.status_code == 200, response.text
        return response.json()["session_id"]

    async def test_refresh_rotates_the_cookie_and_the_row(
        self, client: httpx.AsyncClient, auth_app: Any, fixture_issuer: FixtureIssuer
    ) -> None:
        first_session = await self._login(client, fixture_issuer)
        cookie_name = auth_app.state.settings.session_cookie_name
        original = client.cookies[cookie_name]

        response = await client.post("/api/v1/auth/refresh")
        assert response.status_code == 200, response.text
        rotated = response.json()
        assert rotated["access_token"]
        assert rotated["session_id"] != first_session
        assert client.cookies[cookie_name] != original

        sessionmaker = auth_app.state.sessionmaker
        async with sessionmaker() as session:
            revoked = await session.execute(
                text("SELECT revoked_at IS NOT NULL FROM sessions WHERE id = :id"),
                {"id": uuid.UUID(first_session)},
            )
            assert revoked.scalar() is True, "the presented session must be revoked, not updated in place"

    async def test_a_replayed_refresh_token_is_rejected(
        self, client: httpx.AsyncClient, auth_app: Any, fixture_issuer: FixtureIssuer
    ) -> None:
        await self._login(client, fixture_issuer)
        cookie_name = auth_app.state.settings.session_cookie_name
        stale = client.cookies[cookie_name]

        assert (await client.post("/api/v1/auth/refresh")).status_code == 200

        client.cookies.set(cookie_name, stale)
        replayed = await client.post("/api/v1/auth/refresh")
        assert replayed.status_code == 401

    async def test_refresh_without_a_cookie_is_rejected(self, client: httpx.AsyncClient) -> None:
        client.cookies.clear()
        assert (await client.post("/api/v1/auth/refresh")).status_code == 401

    async def test_the_refresh_grant_is_what_is_sent(
        self, client: httpx.AsyncClient, fixture_issuer: FixtureIssuer
    ) -> None:
        await self._login(client, fixture_issuer)
        await client.post("/api/v1/auth/refresh")
        assert fixture_issuer.script.last_form["grant_type"] == ["refresh_token"]


class TestLogout:
    async def test_logout_revokes_the_session_and_clears_the_cookie(
        self, client: httpx.AsyncClient, auth_app: Any, fixture_issuer: FixtureIssuer
    ) -> None:
        state, nonce = await _begin_login(client)
        fixture_issuer.script.subject = _new_subject(fixture_issuer)
        fixture_issuer.script.nonce = nonce
        opened = (await client.get("/api/v1/auth/callback", params={"code": "c", "state": state})).json()

        response = await client.post("/api/v1/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"status": "logged_out"}

        sessionmaker = auth_app.state.sessionmaker
        async with sessionmaker() as session:
            revoked = await session.execute(
                text("SELECT revoked_at IS NOT NULL FROM sessions WHERE id = :id"),
                {"id": uuid.UUID(opened["session_id"])},
            )
            assert revoked.scalar() is True

        assert (await client.post("/api/v1/auth/refresh")).status_code == 401

    async def test_logout_succeeds_with_no_session_at_all(self, client: httpx.AsyncClient) -> None:
        """§4.4: logout must succeed even when the credential has already expired."""
        client.cookies.clear()
        response = await client.post("/api/v1/auth/logout")
        assert response.status_code == 200
