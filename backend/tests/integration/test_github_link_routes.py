# SPDX-License-Identifier: FSL-1.1-ALv2
"""The GitHub account link, end to end against a real app, a real database and real sockets.

WHY A LOCAL HTTP SERVER RATHER THAN A PATCHED TRANSPORT. §0.4.1 allows a test to substitute a transport
and never a collaborator, and this does not even need the exception: `GITHUB_API_BASE_URL` and
`GITHUB_OAUTH_BASE_URL` are settings the product already has, because a GitHub Enterprise deployment
moves both. So the test points them at a server on loopback and everything else is the real path — the
real `httpx` client, the real header assembly, the real JSON parsing, the real form-encoded exchange.
Nothing in `src/` knows a test is running.

WHAT THESE PIN, in the order they are most likely to break:

* the credential is SEALED at rest and is not the bytes GitHub sent;
* no response body and no audit row contains it;
* one user cannot see another's link, and one tenant cannot see another's, and that is asserted with
  two users who differ only in id and two tenants that differ only in id;
* the unconfigured server — the state of every fresh install — answers with what to set rather than
  with a bare error;
* disconnect deletes locally even when GitHub refuses the revocation, and says which happened.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from src.auth.dependencies import require_principal
from src.auth.principal import Principal, UserRole

from tests.integration.production_app import apply_committed_baseline_env, real_app_lifespan

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

#: Fixed ids, so a failure that depends on ordering is reproducible. Two users in one tenant and one
#: user in a second tenant is the smallest set that can distinguish the two isolation failures.
TENANT_A = uuid.UUID("aaaa1111-2222-3333-4444-555566667777")
TENANT_B = uuid.UUID("bbbb1111-2222-3333-4444-555566667777")
USER_A = uuid.UUID("11110000-0000-0000-0000-00000000000a")
USER_B = uuid.UUID("11110000-0000-0000-0000-00000000000b")
USER_C = uuid.UUID("11110000-0000-0000-0000-00000000000c")

#: The token the fake GitHub hands out. Self-labelling and synthetic; `check-test-credentials` refuses a
#: credential-shaped literal in a test source, so this deliberately looks like what it is.
ISSUED_TOKEN = "github-user-to-server-value-for-this-test-only"
REFRESH_TOKEN = "github-refresh-value-for-this-test-only"

#: Two repositories, one private and one public, with a language on one and none on the other — the
#: absent language is what proves the UI is handed absence rather than a word.
REPOSITORIES: list[dict[str, Any]] = [
    {
        "full_name": "octo-org/deploy-me",
        "name": "deploy-me",
        "owner": {"login": "octo-org"},
        "private": True,
        "default_branch": "main",
        "language": "Python",
        "pushed_at": "2026-09-18T10:11:12Z",
        "clone_url": "https://github.com/octo-org/deploy-me.git",
        "html_url": "https://github.com/octo-org/deploy-me",
        "size": 1024,
        "archived": False,
    },
    {
        "full_name": "octo-cat/notes",
        "name": "notes",
        "owner": {"login": "octo-cat"},
        "private": False,
        "default_branch": "trunk",
        "language": None,
        "pushed_at": "2026-08-01T00:00:00Z",
        "clone_url": "https://github.com/octo-cat/notes.git",
        "html_url": "https://github.com/octo-cat/notes",
        "size": 8,
        "archived": True,
    },
]


class _FakeGitHub(BaseHTTPRequestHandler):
    """The four endpoints the link uses. Records what it was asked, so a test can assert on it."""

    #: Mutated by the fixture, read by the assertions. Class-level because the handler is instantiated
    #: per request by `ThreadingHTTPServer`.
    calls: list[tuple[str, str]] = []
    revoke_status: int = 204
    token_error: str | None = None

    def log_message(self, *args: Any) -> None:  # noqa: A002 - signature is the library's
        """Silence. The default handler writes every request to stderr and drowns pytest output."""

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - the library's naming
        type(self).calls.append(("POST", self.path))
        if self.path.startswith("/login/oauth/access_token"):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if type(self).token_error:
                # GitHub answers 200 with an error object, which is the case worth pinning: a naive
                # client reads the 200 and proceeds with no token.
                self._json(200, {"error": type(self).token_error})
                return
            self._json(
                200,
                {
                    "access_token": ISSUED_TOKEN,
                    "expires_in": 28800,
                    "refresh_token": REFRESH_TOKEN,
                    "refresh_token_expires_in": 15897600,
                    "token_type": "bearer",
                    "scope": "",
                },
            )
            return
        self._json(404, {"message": "not found"})

    def do_GET(self) -> None:  # noqa: N802 - the library's naming
        type(self).calls.append(("GET", self.path))
        authorization = self.headers.get("Authorization") or ""
        if not authorization.endswith(ISSUED_TOKEN):
            # The credential has to arrive, and it has to be the one that was issued. Without this the
            # test would pass against an implementation that sent no token at all.
            self._json(401, {"message": "bad credentials"})
            return
        if self.path == "/user":
            self._json(200, {"login": "octo-cat", "id": 4242, "avatar_url": "https://example.invalid/a.png"})
            return
        if self.path.startswith("/user/repos"):
            self._json(200, REPOSITORIES)
            return
        if self.path.startswith("/repos/"):
            # The single-repository read `POST /projects/from-github` makes before writing anything.
            # Answered from the same two rows the listing serves, so a test cannot pick a repository
            # the listing never offered — and an unknown one gets GitHub's own 404.
            wanted = self.path[len("/repos/") :]
            for entry in REPOSITORIES:
                if entry["full_name"] == wanted:
                    self._json(200, entry)
                    return
            self._json(404, {"message": "Not Found"})
            return
        self._json(404, {"message": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802 - the library's naming
        type(self).calls.append(("DELETE", self.path))
        self.send_response(type(self).revoke_status)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def fake_github() -> Iterator[str]:
    """A GitHub on loopback, on a port the OS chose. Returns its base URL."""
    _FakeGitHub.calls = []
    _FakeGitHub.revoke_status = 204
    _FakeGitHub.token_error = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGitHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _principal(user_id: uuid.UUID, tenant_id: uuid.UUID) -> Principal:
    return Principal.for_user(
        user_id=user_id,
        subject=f"sub-{user_id}",
        email=f"{user_id.hex}@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=tenant_id,
    )


class _Switchable:
    """Which principal the app sees. A mutable holder rather than a re-created app per user.

    Re-creating the app per user would also re-create the schema fixture and the Redis client, and the
    property under test is precisely that ONE running deployment keeps two users' links apart.
    """

    def __init__(self) -> None:
        self.current = _principal(USER_A, TENANT_A)

    def __call__(self) -> Principal:
        return self.current


@pytest_asyncio.fixture
async def link_app(
    monkeypatch: pytest.MonkeyPatch, schema_at_head: str, fake_github: str
) -> AsyncIterator[tuple[Any, _Switchable]]:
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    redis_url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if redis_url:
        monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("APP_ENV", "test")
    # CONFIGURED, pointing at loopback. The unconfigured case gets its own fixture below, because
    # "what does a fresh install say" is a different question from "does the flow work".
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "Iv1.testclientid")
    monkeypatch.setenv("GITHUB_APP_OAUTH_CREDENTIAL", "github-client-secret-for-this-test-only")
    monkeypatch.setenv("GITHUB_OAUTH_BASE_URL", fake_github)
    monkeypatch.setenv("GITHUB_API_BASE_URL", fake_github)

    app = create_app()
    switch = _Switchable()
    app.dependency_overrides[require_principal] = switch
    async with real_app_lifespan(app):
        await _seed_users(app)
        yield app, switch
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def unconfigured_app(monkeypatch: pytest.MonkeyPatch, schema_at_head: str) -> AsyncIterator[Any]:
    """The state of every fresh install: no client credentials at all."""
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    redis_url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if redis_url:
        monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "")
    monkeypatch.setenv("GITHUB_APP_OAUTH_CREDENTIAL", "")

    app = create_app()
    app.dependency_overrides[require_principal] = lambda: _principal(USER_A, TENANT_A)
    async with real_app_lifespan(app):
        await _seed_users(app)
        yield app
    app.dependency_overrides.clear()


async def _seed_users(app: Any) -> None:
    """Three real `users` rows, and an empty link table.

    Inserted rather than created through a route: there is no user-creation endpoint — users arrive
    through the OIDC callback — and driving a full OIDC round trip to obtain a row would make this test
    about authentication, which it deliberately is not.

    THE DELETE MATTERS AS MUCH AS THE INSERT. `schema_at_head` is shared across the module, so without
    it a row written by one test is visible to the next, and three assertions about what is stored
    passed or failed depending on collection order. An order-dependent assertion about storage is worse
    than none: it reports the previous test's state as this one's.
    """
    async with app.state.sessionmaker() as session:
        await session.execute(text("DELETE FROM github_account_links"))
        for user_id in (USER_A, USER_B, USER_C):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, name, role, idp_subject, is_active) "
                    "VALUES (:id, :email, 'Link Test', 'admin', :sub, true) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": user_id, "email": f"{user_id.hex}@example.invalid", "sub": f"sub-{user_id}"},
            )
        await session.commit()


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _connect(client: AsyncClient) -> str:
    """Run the whole flow and return the login. The `state` is taken from the URL the server returned."""
    begin = await client.post("/api/v1/integrations/github/connect")
    assert begin.status_code == 200, begin.text
    authorize_url = begin.json()["authorize_url"]
    state = authorize_url.split("state=")[1].split("&")[0]
    callback = await client.get(
        "/api/v1/integrations/github/callback", params={"code": "the-authorization-code", "state": state}
    )
    assert callback.status_code == 200, callback.text
    return str(callback.json()["login"])


class TestTheLinkIsStoredSealed:
    async def test_connecting_stores_a_link_and_returns_no_token(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            login = await _connect(client)
            assert login == "octo-cat"

            status = await client.get("/api/v1/integrations/github")
            assert status.status_code == 200, status.text
            body = status.json()
            assert body["configured"] is True
            assert body["connected"] is True
            assert body["login"] == "octo-cat"
            # NO TOKEN ANYWHERE IN THE BODY. Asserted over the serialised response rather than field by
            # field, so a field added later cannot carry it past this test.
            assert ISSUED_TOKEN not in status.text
            assert REFRESH_TOKEN not in status.text
            # Tri-state: the link has never been used, which is not the same as failing.
            assert body["last_use_ok"] is None

    async def test_the_stored_column_is_ciphertext_and_not_the_token(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            await _connect(client)
        async with app.state.sessionmaker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT access_token_sealed, refresh_token_sealed, github_user_id "
                            "FROM github_account_links WHERE user_id = :u"
                        ),
                        {"u": USER_A},
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None
        sealed = bytes(row["access_token_sealed"])
        assert ISSUED_TOKEN.encode("utf-8") not in sealed
        assert bytes(row["refresh_token_sealed"]) != REFRESH_TOKEN.encode("utf-8")
        # The nonce is prefixed, so the ciphertext is longer than the plaintext by nonce + tag.
        assert len(sealed) >= len(ISSUED_TOKEN) + 12 + 16
        assert int(row["github_user_id"]) == 4242

    async def test_the_audit_row_names_the_account_and_not_the_token(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            await _connect(client)
        async with app.state.sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT action, reason, after_state::text AS after FROM audit_events "
                            "WHERE action = 'github_account_linked' ORDER BY seq DESC LIMIT 1"
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert rows, "linking must be audited"
        record = rows[0]
        assert "octo-cat" in record["reason"]
        assert ISSUED_TOKEN not in record["reason"]
        assert ISSUED_TOKEN not in (record["after"] or "")
        assert REFRESH_TOKEN not in (record["after"] or "")


class TestIsolation:
    async def test_one_users_link_is_invisible_to_another_user(self, link_app: Any) -> None:
        app, switch = link_app
        async with await _client(app) as client:
            await _connect(client)

            switch.current = _principal(USER_B, TENANT_A)
            status = await client.get("/api/v1/integrations/github")
            assert status.status_code == 200
            assert status.json()["connected"] is False, "user B must not inherit user A's link"

            repositories = await client.get("/api/v1/integrations/github/repositories")
            assert repositories.status_code == 409, repositories.text
            assert "No GitHub account" in repositories.text or "no GitHub" in repositories.text.lower()

    async def test_the_same_user_id_in_another_tenant_sees_nothing(self, link_app: Any) -> None:
        """The tenant half of the predicate, isolated from the user half.

        USER_A in TENANT_B is not a realistic principal — it is the shape a broken tenant predicate
        produces, which is exactly why it is the thing to test. If `read` matched on `user_id` alone
        this returns connected.
        """
        app, switch = link_app
        async with await _client(app) as client:
            await _connect(client)

            switch.current = _principal(USER_A, TENANT_B)
            status = await client.get("/api/v1/integrations/github")
            assert status.json()["connected"] is False

            disconnect = await client.delete("/api/v1/integrations/github")
            assert disconnect.status_code == 409, "a cross-tenant disconnect must refuse"

        # And the row is still there, so the refusal was a refusal rather than a silent no-op delete.
        async with app.state.sessionmaker() as session:
            remaining = (
                await session.execute(
                    text("SELECT count(*) FROM github_account_links WHERE user_id = :u"), {"u": USER_A}
                )
            ).scalar_one()
        assert remaining == 1

    async def test_a_transplanted_ciphertext_does_not_open(self, link_app: Any) -> None:
        """The second, independent half of the isolation guarantee: the seal is bound to the user.

        The row is moved to another user by hand — which is what a dropped predicate or a bad backup
        restore looks like — and the listing must refuse rather than serve someone else's repositories.
        """
        app, switch = link_app
        async with await _client(app) as client:
            await _connect(client)

        async with app.state.sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO github_account_links (user_id, tenant_id, github_login, github_user_id, "
                    "access_token_sealed) SELECT :new, tenant_id, github_login, github_user_id, "
                    "access_token_sealed FROM github_account_links WHERE user_id = :old"
                ),
                {"new": USER_C, "old": USER_A},
            )
            await session.commit()

        async with await _client(app) as client:
            switch.current = _principal(USER_C, TENANT_A)
            response = await client.get("/api/v1/integrations/github/repositories")
        assert response.status_code >= 400, "a transplanted seal must not open"
        assert ISSUED_TOKEN not in response.text


class TestRepositoryListing:
    async def test_the_listing_is_real_searchable_and_paged(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            await _connect(client)

            everything = await client.get("/api/v1/integrations/github/repositories")
            assert everything.status_code == 200, everything.text
            page = everything.json()
            assert page["total"] == 2
            assert page["truncated"] is False
            names = [item["full_name"] for item in page["items"]]
            assert names == ["octo-org/deploy-me", "octo-cat/notes"]
            # Absence is absence: GitHub reported no language for the second repository.
            assert page["items"][1]["language"] == ""
            assert page["items"][0]["private"] is True
            assert page["items"][1]["default_branch"] == "trunk"

            filtered = await client.get("/api/v1/integrations/github/repositories", params={"query": "deploy"})
            assert [item["full_name"] for item in filtered.json()["items"]] == ["octo-org/deploy-me"]

            paged = await client.get("/api/v1/integrations/github/repositories", params={"per_page": 1, "page": 2})
            assert paged.json()["total"] == 2
            assert [item["full_name"] for item in paged.json()["items"]] == ["octo-cat/notes"]

    async def test_a_successful_listing_records_that_it_worked(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            await _connect(client)
            await client.get("/api/v1/integrations/github/repositories")
            status = await client.get("/api/v1/integrations/github")
        body = status.json()
        assert body["last_use_ok"] is True
        assert "2 repository" in body["last_use_detail"]


class TestDisconnect:
    async def test_disconnect_deletes_and_revokes(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            await _connect(client)
            response = await client.delete("/api/v1/integrations/github")
            assert response.status_code == 200, response.text
            assert response.json() == {
                "login": "octo-cat",
                "revoked_at_github": True,
                "credential_kind": "oauth_app",
            }

            after = await client.get("/api/v1/integrations/github")
            assert after.json()["connected"] is False

        assert any(method == "DELETE" for method, _ in _FakeGitHub.calls), "revocation must be attempted"

        async with app.state.sessionmaker() as session:
            remaining = (await session.execute(text("SELECT count(*) FROM github_account_links"))).scalar_one()
        assert remaining == 0

    async def test_a_failed_revocation_still_disconnects_and_says_so(self, link_app: Any) -> None:
        """A user who wants to revoke access must not be blocked by GitHub being unreachable."""
        app, _ = link_app
        async with await _client(app) as client:
            await _connect(client)
            _FakeGitHub.revoke_status = 500
            response = await client.delete("/api/v1/integrations/github")
            assert response.status_code == 200, response.text
            assert response.json()["revoked_at_github"] is False

        async with app.state.sessionmaker() as session:
            remaining = (await session.execute(text("SELECT count(*) FROM github_account_links"))).scalar_one()
            reason = (
                await session.execute(
                    text(
                        "SELECT reason FROM audit_events WHERE action = 'github_account_unlinked' "
                        "ORDER BY seq DESC LIMIT 1"
                    )
                )
            ).scalar_one()
        assert remaining == 0
        assert "could not be revoked" in reason

    async def test_disconnecting_without_a_link_refuses_by_name(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            response = await client.delete("/api/v1/integrations/github")
        assert response.status_code == 409
        assert response.json()["type"].endswith("github-link-absent")


class TestLinkingWithAPastedToken:
    """The redirect-free path: no authorize URL, no account chooser, no GitHub UI at all.

    It is the path that works on a deployment with NO GitHub App, which is why `unconfigured_app` is the
    fixture here rather than `link_app` — proving the capability exactly where the other path cannot
    work. The fake GitHub is still on loopback, because the token is verified against the real API
    surface before anything is stored.
    """

    async def test_a_verified_token_links_without_any_redirect(
        self, unconfigured_app: Any, fake_github: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The App credentials stay empty; only the API base URL points at the fake, which is the same
        # setting a GitHub Enterprise deployment sets.
        unconfigured_app.state.github_link_service._users.api_base_url = fake_github  # noqa: SLF001

        async with await _client(unconfigured_app) as client:
            response = await client.put("/api/v1/integrations/github/token", json={"token": ISSUED_TOKEN})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["connected"] is True
            assert body["login"] == "octo-cat"
            assert body["credential_kind"] == "personal_token"
            # The deployment still has no App, and the screen must keep saying so rather than implying
            # the link came from one.
            assert body["configured"] is False
            assert body["token_link_available"] is True
            assert ISSUED_TOKEN not in response.text

            # And no authorize URL was ever produced: `/connect` still refuses, which is the proof that
            # this link did not go through GitHub's UI.
            begin = await client.post("/api/v1/integrations/github/connect")
            assert begin.status_code == 503

        async with unconfigured_app.state.sessionmaker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT credential_kind, access_token_sealed, refresh_token_sealed "
                            "FROM github_account_links WHERE user_id = :u"
                        ),
                        {"u": USER_A},
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None
        assert row["credential_kind"] == "personal_token"
        assert ISSUED_TOKEN.encode("utf-8") not in bytes(row["access_token_sealed"])
        # No refresh token exists for a pasted one, and inventing one would make a refresh attempt
        # possible against a credential that cannot be refreshed.
        assert row["refresh_token_sealed"] is None

    async def test_a_token_github_refuses_is_not_stored(self, unconfigured_app: Any, fake_github: str) -> None:
        unconfigured_app.state.github_link_service._users.api_base_url = fake_github  # noqa: SLF001

        async with await _client(unconfigured_app) as client:
            response = await client.put(
                "/api/v1/integrations/github/token", json={"token": "a-token-this-server-will-refuse"}
            )

        assert response.status_code == 502, response.text
        assert "401" in response.text
        # The remedy is named: a new token, and what it needs to grant.
        assert "Contents and Metadata" in response.text

        async with unconfigured_app.state.sessionmaker() as session:
            stored = (await session.execute(text("SELECT count(*) FROM github_account_links"))).scalar_one()
        assert stored == 0

    async def test_pasting_twice_leaves_one_link(self, unconfigured_app: Any, fake_github: str) -> None:
        unconfigured_app.state.github_link_service._users.api_base_url = fake_github  # noqa: SLF001

        async with await _client(unconfigured_app) as client:
            first = await client.put("/api/v1/integrations/github/token", json={"token": ISSUED_TOKEN})
            second = await client.put("/api/v1/integrations/github/token", json={"token": ISSUED_TOKEN})

        assert (first.status_code, second.status_code) == (200, 200)
        async with unconfigured_app.state.sessionmaker() as session:
            stored = (await session.execute(text("SELECT count(*) FROM github_account_links"))).scalar_one()
        assert stored == 1

    async def test_disconnecting_a_pasted_token_says_it_must_be_deleted_on_github(
        self, unconfigured_app: Any, fake_github: str
    ) -> None:
        """The honest difference: this server cannot revoke a token the person made."""
        unconfigured_app.state.github_link_service._users.api_base_url = fake_github  # noqa: SLF001

        async with await _client(unconfigured_app) as client:
            await client.put("/api/v1/integrations/github/token", json={"token": ISSUED_TOKEN})
            response = await client.delete("/api/v1/integrations/github")

        assert response.status_code == 200, response.text
        assert response.json() == {
            "login": "octo-cat",
            "revoked_at_github": False,
            "credential_kind": "personal_token",
        }
        # No revocation was ATTEMPTED, because it cannot succeed and a 404 recorded as "not revoked"
        # would be true for the wrong reason.
        assert not any(method == "DELETE" for method, _ in _FakeGitHub.calls)

        async with unconfigured_app.state.sessionmaker() as session:
            reason = (
                await session.execute(
                    text(
                        "SELECT reason FROM audit_events WHERE action = 'github_account_unlinked' "
                        "ORDER BY seq DESC LIMIT 1"
                    )
                )
            ).scalar_one()
        assert "only be deleted on GitHub" in reason

    async def test_a_short_paste_is_refused_before_github_is_called(self, unconfigured_app: Any) -> None:
        async with await _client(unconfigured_app) as client:
            response = await client.put("/api/v1/integrations/github/token", json={"token": "short"})

        assert response.status_code == 422, response.text

    async def test_the_listing_works_through_a_pasted_token(self, unconfigured_app: Any, fake_github: str) -> None:
        """The point of linking at all. An unconfigured deployment must still list repositories."""
        service = unconfigured_app.state.github_link_service
        service._users.api_base_url = fake_github  # noqa: SLF001

        async with await _client(unconfigured_app) as client:
            await client.put("/api/v1/integrations/github/token", json={"token": ISSUED_TOKEN})
            response = await client.get("/api/v1/integrations/github/repositories")

        assert response.status_code == 200, response.text
        assert [item["full_name"] for item in response.json()["items"]] == [
            "octo-org/deploy-me",
            "octo-cat/notes",
        ]


class TestCreatingAProjectFromAPickedRepository:
    """`POST /projects/from-github`, which replaced the three typed fields nobody could fill in.

    WHAT THESE PIN: the project points at the path the user chose, it says `awaiting_clone` rather than
    looking like an ordinary empty project, the repository is CONFIRMED against the API before anything
    is written, and the credential appears in no row.
    """

    async def test_it_creates_a_project_awaiting_its_clone(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            await _connect(client)

            created = await client.post(
                "/api/v1/projects/from-github",
                json={
                    "repo_full_name": "octo-org/deploy-me",
                    "parent_directory": "/srv/workspaces",
                    "directory_name": "",
                    "branch": "",
                },
            )
            assert created.status_code == 201, created.text
            body = created.json()
            # The name comes from the repository, and the path from the parent plus the repository name.
            assert body["name"] == "deploy-me"
            assert body["path"] == "/srv/workspaces/deploy-me"
            assert body["settings"]["clone_state"] == "awaiting_clone"
            assert body["settings"]["repo_default_branch"] == "main"
            assert body["settings"]["repo_private"] is True
            assert ISSUED_TOKEN not in created.text

            # AND THE INDEX STATUS SAYS SO. `empty` would be the same number of indexed files and a
            # completely different next step.
            status = await client.get(f"/api/v1/analysis/codebase/{body['id']}/status")
            assert status.status_code == 200, status.text
            assert status.json()["status"] == "awaiting_clone"

    async def test_a_repository_the_account_cannot_read_is_refused_before_anything_is_written(
        self, link_app: Any
    ) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            await _connect(client)

            refused = await client.post(
                "/api/v1/projects/from-github",
                json={"repo_full_name": "someone-else/private-thing", "parent_directory": "/srv"},
            )

        assert refused.status_code == 502, refused.text
        assert "404" in refused.text
        # The remedy is named rather than left to be guessed.
        assert "renamed" in refused.text or "private" in refused.text

        async with app.state.sessionmaker() as session:
            projects = (
                await session.execute(text("SELECT count(*) FROM projects WHERE name = 'private-thing'"))
            ).scalar_one()
        assert projects == 0

    async def test_it_refuses_without_a_linked_account(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            response = await client.post(
                "/api/v1/projects/from-github",
                json={"repo_full_name": "octo-org/deploy-me", "parent_directory": "/srv"},
            )
        assert response.status_code == 409, response.text
        assert response.json()["type"].endswith("github-link-absent")

    async def test_the_credential_reaches_no_project_row_or_audit_row(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            await _connect(client)
            created = await client.post(
                "/api/v1/projects/from-github",
                json={"repo_full_name": "octo-org/deploy-me", "parent_directory": "/srv/ws"},
            )
            project_id = created.json()["id"]

        async with app.state.sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT to_jsonb(projects) AS p FROM projects WHERE id = :id",
                        ),
                        {"id": project_id},
                    )
                )
                .mappings()
                .all()
            )
            audit = (
                (
                    await session.execute(
                        text("SELECT to_jsonb(audit_events) AS a FROM audit_events WHERE project_id = :id"),
                        {"id": project_id},
                    )
                )
                .mappings()
                .all()
            )
        serialised = json.dumps([dict(r) for r in rows + audit], default=str)
        assert ISSUED_TOKEN not in serialised
        # Not vacuous: the row really is there and names the repository.
        assert "deploy-me" in serialised

    async def test_cloning_a_local_project_is_refused_by_name(self, link_app: Any) -> None:
        """A project created with a typed path has a directory already; there is nothing to clone."""
        app, _ = link_app
        async with await _client(app) as client:
            local = await client.post(
                "/api/v1/projects",
                json={"name": "typed", "path": "/srv/typed", "repo_url": None, "settings": {}},
            )
            assert local.status_code == 201, local.text

            response = await client.post(f"/api/v1/projects/{local.json()['id']}/clone")

        assert response.status_code == 502, response.text
        assert "not created from a GitHub repository" in response.text

    async def test_the_typed_local_path_route_still_works(self, link_app: Any) -> None:
        """Part 3 keeps the local option alongside; a regression here would break onboarding."""
        app, _ = link_app
        async with await _client(app) as client:
            created = await client.post(
                "/api/v1/projects",
                json={"name": "local-one", "path": "/srv/local-one", "repo_url": None, "settings": {}},
            )
            assert created.status_code == 201, created.text
            assert created.json()["path"] == "/srv/local-one"
            # And it is NOT awaiting a clone: the directory is the user's own statement about their disk.
            status = await client.get(f"/api/v1/analysis/codebase/{created.json()['id']}/status")
        assert status.json()["status"] == "empty"


class TestTheUnconfiguredServer:
    async def test_the_status_route_explains_what_to_configure(self, unconfigured_app: Any) -> None:
        """A 200 with instructions, not a 503: this is the route a screen loads to decide what to draw."""
        async with await _client(unconfigured_app) as client:
            response = await client.get("/api/v1/integrations/github")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["configured"] is False
        assert body["connected"] is False
        assert "GITHUB_APP_CLIENT_ID" in body["configuration_hint"]
        assert "GITHUB_APP_OAUTH_CREDENTIAL" in body["configuration_hint"]
        assert "callback" in body["configuration_hint"]

    async def test_connecting_refuses_with_the_variables_to_set(self, unconfigured_app: Any) -> None:
        async with await _client(unconfigured_app) as client:
            response = await client.post("/api/v1/integrations/github/connect")
        assert response.status_code == 503, response.text
        assert "GITHUB_APP_CLIENT_ID" in response.text
        assert "GITHUB_APP_OAUTH_CREDENTIAL" in response.text

    async def test_listing_refuses_because_nothing_is_linked_and_names_both_ways_to_link(
        self, unconfigured_app: Any
    ) -> None:
        """A 409, not a 503, and that is a correction.

        The route used to refuse with "the server is not configured", which was false about what the
        caller asked for: a token pasted into ForgeOps needs no GitHub App, so an unconfigured
        deployment can hold a working link. What is missing here is a LINK, so that is what the refusal
        says — and it names both ways to make one, including the one that works right now.
        """
        async with await _client(unconfigured_app) as client:
            response = await client.get("/api/v1/integrations/github/repositories")
        assert response.status_code == 409, response.text
        assert "No GitHub account is linked" in response.text
        assert "pasting a GitHub token" in response.text
        assert "GITHUB_APP_CLIENT_ID" in response.text


class TestTheCallbackCannotBeReplayedOrForged:
    async def test_a_state_is_single_use(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            begin = await client.post("/api/v1/integrations/github/connect")
            state = begin.json()["authorize_url"].split("state=")[1].split("&")[0]
            first = await client.get("/api/v1/integrations/github/callback", params={"code": "c", "state": state})
            assert first.status_code == 200, first.text
            replay = await client.get("/api/v1/integrations/github/callback", params={"code": "c", "state": state})
        assert replay.status_code == 502, replay.text
        assert "single-use" in replay.text

    async def test_an_unknown_state_is_refused(self, link_app: Any) -> None:
        app, _ = link_app
        async with await _client(app) as client:
            response = await client.get(
                "/api/v1/integrations/github/callback",
                params={"code": "c", "state": "a-state-this-server-never-issued"},
            )
        assert response.status_code == 502
        assert "could not be matched" in response.text

    async def test_githubs_error_object_in_a_200_is_treated_as_a_failure(self, link_app: Any) -> None:
        """GitHub answers 200 with `{"error": ...}`; a client that reads the status proceeds tokenless."""
        app, _ = link_app
        async with await _client(app) as client:
            begin = await client.post("/api/v1/integrations/github/connect")
            state = begin.json()["authorize_url"].split("state=")[1].split("&")[0]
            _FakeGitHub.token_error = "bad_verification_code"
            response = await client.get("/api/v1/integrations/github/callback", params={"code": "c", "state": state})
        assert response.status_code == 502, response.text
        assert "bad_verification_code" in response.text

        async with app.state.sessionmaker() as session:
            stored = (await session.execute(text("SELECT count(*) FROM github_account_links"))).scalar_one()
        assert stored == 0, "a failed exchange must store nothing"
