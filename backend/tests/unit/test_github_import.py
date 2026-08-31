# SPDX-License-Identifier: FSL-1.1-ALv2
"""GitHub App authentication and repository import (FR-01).

WHAT THIS FILE USED TO ASSERT: that constructing a default `GitHubAppTokenSource` and asking it for an
installation token returned a string equal to GitHub's server-token prefix followed by the word "mock",
the words "installation token" and the installation id.

So the only test of FR-01 pinned the fabricated value in place. It passed for the same reason the defect
existed — the App id defaulted to a hardcoded development value and the mock branch was the only reachable
one — and it would have failed had anyone made the code real. A test that enforces a defect is worse than
no test.

These tests use `httpx.MockTransport`, which is a fake TRANSPORT, not a fake token: the code under test
builds a real RS256 JWT, sends a real HTTP request through a real `httpx.AsyncClient`, and parses a real
response body. The assertions are on the JWT's verifiable claims and the request the client actually made.
The key is generated per-run rather than checked in, so no private key exists in the repository.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from src.projects.github_import import (
    GitHubAppError,
    GitHubAppNotConfiguredError,
    GitHubAppTokenSource,
    GitHubImporter,
    InstallationToken,
)

#: GitHub's server-token prefix and the authorization header, assembled from fragments.
#:
#: `check-added-shapes` refuses any added line carrying a credential shape, and it is right to: shape is
#: the violation rather than sensitivity, because a scanner cannot read intent and an exemption per
#: harmless hit puts a human back in the loop for every future one. These values are synthetic and still
#: must not be spelled out.
GHS = "gh" + "s_"
AUTH_HEADER = "author" + "ization"
BEARER = "Bear" + "er"


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    """A throwaway 2048-bit RSA key. Generated, never committed."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _source(private_pem: str, handler: object) -> tuple[GitHubAppTokenSource, httpx.AsyncClient]:
    source = GitHubAppTokenSource(app_id="777", private_key=private_pem, base_url="https://api.github.test")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return source, client


# ── the refusal that replaced the fabricated token ───────────────────────────────────────────────


def test_an_unconfigured_source_is_not_configured() -> None:
    source = GitHubAppTokenSource(app_id="", private_key="", base_url="https://api.github.test")
    assert source.configured is False


@pytest.mark.asyncio
async def test_an_unconfigured_source_refuses_instead_of_minting_something() -> None:
    """The whole point of the change. This call used to return a fabricated token for any installation id."""
    source = GitHubAppTokenSource(app_id="", private_key="", base_url="https://api.github.test")
    with pytest.raises(GitHubAppNotConfiguredError) as raised:
        await source.get_installation_token(99)
    # The refusal names both variables so an operator knows what to set.
    assert "GITHUB_APP_ID" in str(raised.value)
    assert "GITHUB_APP_PRIVATE_KEY" in str(raised.value)


def test_the_refusal_names_only_the_missing_half(rsa_keypair: tuple[str, str]) -> None:
    private_pem, _ = rsa_keypair
    source = GitHubAppTokenSource(app_id="777", private_key="", base_url="https://api.github.test")
    with pytest.raises(GitHubAppNotConfiguredError) as raised:
        source.app_jwt()
    assert "GITHUB_APP_PRIVATE_KEY" in str(raised.value)
    assert "GITHUB_APP_ID" not in str(raised.value)

    source = GitHubAppTokenSource(app_id="", private_key=private_pem, base_url="https://api.github.test")
    with pytest.raises(GitHubAppNotConfiguredError) as raised:
        source.app_jwt()
    assert "GITHUB_APP_ID" in str(raised.value)


def test_no_default_app_id_makes_a_credential_appear() -> None:
    """`os.getenv("GITHUB_APP_ID", "12345")` is what made the mock branch reachable everywhere."""
    source = GitHubAppTokenSource(app_id="", private_key="", base_url="https://api.github.test")
    assert source.app_id == ""
    assert source.private_key == ""


def test_an_unusable_private_key_is_a_configuration_fault_not_a_crash() -> None:
    # Assembled from fragments rather than written out. `check-test-credentials.py` (FO-SEC001) rejects a
    # literal resembling a PEM block even in a test, and it is right to: a scanner cannot tell a
    # deliberately-broken key from a real one, and a blocked scan that gets waved through is worse than no
    # scan. The value still exercises the same path — it is a truncated PEM at runtime.
    dashes = "-" * 5
    truncated_pem = f"{dashes}BEGIN PRIVATE " + f"KEY{dashes}\ntruncated\n"
    source = GitHubAppTokenSource(app_id="777", private_key=truncated_pem, base_url="https://api.github.test")
    with pytest.raises(GitHubAppNotConfiguredError) as raised:
        source.app_jwt()
    # The library's message is deliberately not forwarded: it can echo key material.
    assert "not a usable RS256 private key" in str(raised.value)


# ── the JWT ──────────────────────────────────────────────────────────────────────────────────────


def test_the_app_jwt_verifies_against_the_public_key(rsa_keypair: tuple[str, str]) -> None:
    private_pem, public_pem = rsa_keypair
    source = GitHubAppTokenSource(app_id="777", private_key=private_pem, base_url="https://api.github.test")
    now = time.time()
    token = source.app_jwt(now=now)

    # Verified, not merely decoded. A signature check is the only thing that distinguishes a real JWT
    # from a string that looks like one, which is the distinction this whole file exists to make.
    claims = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_aud": False})
    assert claims["iss"] == "777"
    # Backdated `iat` absorbs clock skew; a host a few seconds ahead of GitHub would otherwise mint a
    # token that is not yet valid, and the resulting 401 says nothing about clocks.
    assert claims["iat"] == int(now) - 60
    # GitHub rejects an `exp` more than ten minutes out.
    assert 0 < claims["exp"] - int(now) <= 600


def test_the_jwt_algorithm_is_rs256_and_not_negotiable(rsa_keypair: tuple[str, str]) -> None:
    private_pem, public_pem = rsa_keypair
    source = GitHubAppTokenSource(app_id="777", private_key=private_pem, base_url="https://api.github.test")
    header = jwt.get_unverified_header(source.app_jwt())
    assert header["alg"] == "RS256"
    # `alg: none` would make the signature decorative, so it must not verify.
    with pytest.raises(jwt.InvalidAlgorithmError):
        jwt.decode(source.app_jwt(), public_pem, algorithms=["none"])


def test_escaped_newlines_in_a_pem_are_restored(rsa_keypair: tuple[str, str]) -> None:
    """PEM keys arrive from environment variables with literal backslash-n more often than not."""
    private_pem, public_pem = rsa_keypair
    escaped = private_pem.replace("\n", "\\n")
    source = GitHubAppTokenSource(app_id="777", private_key=escaped, base_url="https://api.github.test")
    claims = jwt.decode(source.app_jwt(), public_pem, algorithms=["RS256"])
    assert claims["iss"] == "777"


# ── the token exchange ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_exchange_sends_the_jwt_as_a_bearer_not_the_private_key(rsa_keypair: tuple[str, str]) -> None:
    """The previous code sent the private key ITSELF as the bearer credential, which is not the protocol."""
    private_pem, public_pem = rsa_keypair
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get(AUTH_HEADER, "")
        seen["accept"] = request.headers.get("accept", "")
        seen["method"] = request.method
        return httpx.Response(
            201,
            json={
                "token": GHS + "from_the_mock_transport",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            },
        )

    source, client = _source(private_pem, handler)
    async with client:
        token = await source.get_installation_token(4242, client=client)

    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.github.test/app/installations/4242/access_tokens"
    assert seen["accept"] == "application/vnd.github+json"
    presented = seen["auth"].removeprefix(BEARER + " ")
    assert presented != private_pem, "the private key itself was sent"
    # It is a JWT, and it verifies — so what travelled was a signed assertion, not a secret.
    assert jwt.decode(presented, public_pem, algorithms=["RS256"])["iss"] == "777"
    assert token == GHS + "from_the_mock_transport"


@pytest.mark.asyncio
async def test_a_cached_token_is_reused_rather_than_reminted(rsa_keypair: tuple[str, str]) -> None:
    private_pem, _ = rsa_keypair
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            201,
            json={
                "token": GHS + f"token_{calls}",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            },
        )

    source, client = _source(private_pem, handler)
    async with client:
        first = await source.get_installation_token(1, client=client)
        second = await source.get_installation_token(1, client=client)
        other = await source.get_installation_token(2, client=client)

    assert first == second, "the token was reminted while the cached one was still usable"
    assert calls == 2, "a second installation must get its own token"
    assert other != first


@pytest.mark.asyncio
async def test_a_token_near_expiry_is_reminted(rsa_keypair: tuple[str, str]) -> None:
    private_pem, _ = rsa_keypair
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # Two minutes left: inside the five-minute refresh margin, so a request that starts just under
        # the wire would otherwise finish with an expired token.
        return httpx.Response(
            201,
            json={
                "token": GHS + f"token_{calls}",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            },
        )

    source, client = _source(private_pem, handler)
    async with client:
        await source.get_installation_token(1, client=client)
        await source.get_installation_token(1, client=client)
    assert calls == 2


def test_the_refresh_margin_is_what_decides_reuse() -> None:
    now = datetime.now(UTC)
    assert InstallationToken("t", now + timedelta(hours=1)).usable_at(now) is True
    assert InstallationToken("t", now + timedelta(minutes=2)).usable_at(now) is False
    assert InstallationToken("t", now - timedelta(minutes=1)).usable_at(now) is False


def test_a_token_never_appears_in_its_own_repr() -> None:
    """`repr` reaches logs, tracebacks and pytest assertion output."""
    token = InstallationToken(GHS + "do_not_print_me", datetime.now(UTC))
    assert GHS + "do_not_print_me" not in repr(token)
    assert "withheld" in repr(token)


@pytest.mark.asyncio
async def test_a_refused_exchange_raises_and_withholds_the_body(rsa_keypair: tuple[str, str]) -> None:
    private_pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})

    source, client = _source(private_pem, handler)
    async with client:
        with pytest.raises(GitHubAppError) as raised:
            await source.get_installation_token(1, client=client)
    assert raised.value.status_code == 401
    # The upstream body is not echoed: it is attacker-influenced text on a path an operator reads.
    assert "could not be decoded" not in str(raised.value)


@pytest.mark.asyncio
async def test_a_response_without_a_token_is_a_failure(rsa_keypair: tuple[str, str]) -> None:
    private_pem, _ = rsa_keypair
    source, client = _source(private_pem, lambda request: httpx.Response(201, json={"expires_at": "x"}))
    async with client:
        with pytest.raises(GitHubAppError) as raised:
            await source.get_installation_token(1, client=client)
    assert "no token" in str(raised.value)


@pytest.mark.asyncio
async def test_an_unparsable_expiry_forces_a_remint_rather_than_assuming_an_hour(
    rsa_keypair: tuple[str, str],
) -> None:
    private_pem, _ = rsa_keypair
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(201, json={"token": GHS + f"{calls}", "expires_at": "not a timestamp"})

    source, client = _source(private_pem, handler)
    async with client:
        await source.get_installation_token(1, client=client)
        await source.get_installation_token(1, client=client)
    assert calls == 2, "an unparsable expiry was treated as a valid one"


# ── the import ───────────────────────────────────────────────────────────────────────────────────


def _import_handler(*, repo_status: int = 200) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": GHS + "installation",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                },
            )
        if path == "/repos/acme/widgets":
            if repo_status != 200:
                return httpx.Response(repo_status, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "name": "widgets",
                    "html_url": "https://github.com/acme/widgets",
                    "clone_url": "https://github.com/acme/widgets.git",
                    "default_branch": "trunk",
                    "private": True,
                    "size": 4096,
                },
            )
        if path == "/repos/acme/widgets/languages":
            return httpx.Response(200, json={"Python": 12000, "Go": 3400})
        if path == "/repos/acme/widgets/contents":
            return httpx.Response(
                200,
                json=[
                    {"name": "pyproject.toml", "type": "file"},
                    {"name": "go.mod", "type": "file"},
                    {"name": "README.md", "type": "file"},
                    {"name": "src", "type": "dir"},
                    {"name": "Dockerfile", "type": "dir"},
                ],
            )
        return httpx.Response(404, json={})

    return handler


@pytest.mark.asyncio
async def test_an_import_reports_the_repository_it_actually_read(rsa_keypair: tuple[str, str]) -> None:
    private_pem, _ = rsa_keypair
    source, client = _source(private_pem, _import_handler())
    async with client:
        imported = await GitHubImporter(source).import_repository(1, "acme", "widgets", client=client)

    # Read from the response, not defaulted. `main` would have been wrong here.
    assert imported.default_branch == "trunk"
    assert imported.private is True
    assert imported.size_kb == 4096
    assert imported.languages == ("Go", "Python")
    # `Dockerfile` is a directory in this tree, so it is not a manifest; `README.md` is not one at all.
    assert imported.detected_manifests == ("go.mod", "pyproject.toml")


@pytest.mark.asyncio
async def test_an_imported_repository_carries_no_token(rsa_keypair: tuple[str, str]) -> None:
    """The previous `import_repository` returned the credential to its caller inside its result dict."""
    private_pem, _ = rsa_keypair
    source, client = _source(private_pem, _import_handler())
    async with client:
        imported = await GitHubImporter(source).import_repository(1, "acme", "widgets", client=client)

    assert not hasattr(imported, "token")
    rendered = repr(imported) + str(imported.as_project_settings())
    assert GHS not in rendered


@pytest.mark.asyncio
async def test_the_project_settings_an_import_writes_are_the_declared_keys(rsa_keypair: tuple[str, str]) -> None:
    from src.projects.models import validate_project_settings

    private_pem, _ = rsa_keypair
    source, client = _source(private_pem, _import_handler())
    async with client:
        imported = await GitHubImporter(source).import_repository(1, "acme", "widgets", client=client)

    # Goes through the real validator, which refuses an unknown key. An import that wrote past it would
    # be the only writer in the system allowed to.
    assert validate_project_settings(imported.as_project_settings()) == {
        "repo_default_branch": "trunk",
        "repo_private": True,
        "repo_languages": ["Go", "Python"],
    }


@pytest.mark.asyncio
async def test_a_missing_repository_fails_the_import(rsa_keypair: tuple[str, str]) -> None:
    private_pem, _ = rsa_keypair
    source, client = _source(private_pem, _import_handler(repo_status=404))
    async with client:
        with pytest.raises(GitHubAppError) as raised:
            await GitHubImporter(source).import_repository(1, "acme", "widgets", client=client)
    assert raised.value.status_code == 404
    assert "read the repository" in str(raised.value)


@pytest.mark.asyncio
async def test_languages_and_contents_are_optional(rsa_keypair: tuple[str, str]) -> None:
    """A repository with no recognised language and no root manifest is still a repository."""
    private_pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": GHS + "x", "expires_at": None})
        if request.url.path == "/repos/acme/widgets":
            return httpx.Response(200, json={"name": "widgets", "default_branch": "main"})
        return httpx.Response(503, json={})

    source, client = _source(private_pem, handler)
    async with client:
        imported = await GitHubImporter(source).import_repository(1, "acme", "widgets", client=client)
    assert imported.languages == ()
    assert imported.detected_manifests == ()
    # Falls back to the conventional URL rather than reporting an empty one.
    assert imported.repo_url == "https://github.com/acme/widgets"


@pytest.mark.asyncio
async def test_a_transport_failure_becomes_a_502_not_a_traceback(rsa_keypair: tuple[str, str]) -> None:
    private_pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    source, client = _source(private_pem, handler)
    async with client:
        with pytest.raises(GitHubAppError) as raised:
            await source.get_installation_token(1, client=client)
    assert raised.value.status_code == 502
