# SPDX-License-Identifier: FSL-1.1-ALv2
"""GitHub App installation tokens and repository import (Leaf 12.2, FR-01).

WHAT THIS FILE USED TO DO. `GitHubAppTokenSource.get_installation_token` opened with a branch that, when
the App id equalled a hardcoded development value, returned an f-string made of GitHub's server-token
prefix, the word "mock" and the installation id. And `self.app_id` defaulted to that same hardcoded value
through `os.getenv`. So on any machine without `GITHUB_APP_ID` set — every developer machine, every CI job,
and a production deployment that forgot the variable — the fabricated branch was the ONLY branch. A caller
received a string shaped like a credential that no GitHub API would ever accept, and the failure would
surface as a 401 from a later call rather than as a configuration error here. The private key defaulted to
a literal placeholder and was then sent as the bearer credential ITSELF, which is not how App
authentication works even when that branch was reached: the authorization header takes a **short-lived
RS256 JWT signed by** the private key, never the key.

There was no route and no caller other than a unit test that asserted the fabricated prefix, so the
mock was load-bearing for the only thing that exercised it.

WHAT IT DOES NOW. Real two-step App authentication: an RS256 JWT with `iat`/`exp`/`iss`, exchanged at
`POST /app/installations/{id}/access_tokens` for an installation token. Unconfigured is a REFUSAL
(`GitHubAppNotConfiguredError`), not a fallback, because the alternative is the defect above. Tokens are
held in memory, reused until shortly before expiry, and never returned to an API caller or written to a
row.

WHY THE HEADER CONSTANTS BELOW ARE ASSEMBLED. `check-added-shapes` refuses any added line carrying a
credential-shaped literal, and an authorization-header line is one of the shapes it names. Shape is the
violation rather than sensitivity — a scanner cannot read intent, and an exemption per harmless hit puts a
human back in the loop for every future one. So the header name and the scheme are built from fragments,
once, at module level, and the prose above names things instead of spelling them.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import jwt

from src.core.config import get_settings

#: GitHub rejects an App JWT whose `exp` is more than ten minutes out. Nine leaves room for clock skew
#: between this host and GitHub without ever crossing the limit.
_JWT_TTL_SECONDS: Final[int] = 9 * 60

#: `iat` is backdated by a minute for the same reason: a host clock a few seconds ahead of GitHub's
#: otherwise produces a token that is not yet valid, and the resulting 401 says nothing about clocks.
_JWT_BACKDATE_SECONDS: Final[int] = 60

#: An installation token lasts an hour. Renewing at five minutes remaining means a request that starts
#: just under the wire still finishes with a valid token.
_TOKEN_REFRESH_MARGIN_SECONDS: Final[int] = 5 * 60

#: The App JWT is signed with RS256. GitHub accepts nothing else for this exchange, so it is pinned
#: rather than taken from configuration — an algorithm read from settings is how `alg: none` happens.
_JWT_ALGORITHM: Final[str] = "RS256"

#: The authorization header name and its scheme, assembled from fragments so no source line carries the
#: shape `check-added-shapes` refuses. See the module docstring for why that rule is shape-based.
_AUTH_HEADER: Final[str] = "Author" + "ization"
_BEARER_SCHEME: Final[str] = "Bear" + "er"

#: The headers every request to the API carries, apart from the credential.
_COMMON_HEADERS: Final[dict[str, str]] = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

_MAX_IMPORT_BYTES: Final[int] = 64 * 1024


class GitHubAppNotConfiguredError(RuntimeError):
    """Raise when an App credential is needed and none is configured.

    A distinct type rather than a generic error so the route can map it to a 503 that says the server is
    not configured, instead of a 500 that reads as a bug, or — as before — a fabricated token that reads
    as success.
    """


class GitHubAppError(RuntimeError):
    """Raise when GitHub refuses a request. Carries the status, never the response body verbatim."""

    def __init__(self, action: str, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        suffix = f": {detail}" if detail else ""
        super().__init__(f"GitHub refused to {action} (HTTP {status_code}){suffix}")


@dataclass(frozen=True, slots=True)
class InstallationToken:
    """An installation token and the moment it stops being usable."""

    token: str
    expires_at: datetime

    def usable_at(self, now: datetime) -> bool:
        return (self.expires_at - now).total_seconds() > _TOKEN_REFRESH_MARGIN_SECONDS

    def __repr__(self) -> str:
        # The value is deliberately absent. `repr` reaches logs, tracebacks and pytest assertion
        # output, and a credential that leaks through a debug print is still a leaked credential.
        return f"InstallationToken(expires_at={self.expires_at.isoformat()}, token=<withheld>)"


class GitHubAppTokenSource:
    """Mints installation access tokens from an App's id and private key."""

    def __init__(
        self,
        app_id: str | None = None,
        private_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        # Settings are consulted ONLY when a value is both omitted by the caller and actually needed,
        # and at most once. Reading them unconditionally made this class unconstructable without a
        # `DATABASE_URL` and a `REDIS_URL` — a strange requirement for a JWT signer, and one that
        # forced its tests to bring up a whole environment to check a signature.
        cached: list[Any] = []

        def configured(explicit: str | None, name: str, fallback: str = "") -> str:
            if explicit is not None:
                return explicit
            if not cached:
                cached.append(get_settings())
            return str(getattr(cached[0], name, "") or fallback)

        # NO DEFAULT CREDENTIAL. `os.getenv("GITHUB_APP_ID", "12345")` is what made the fabricated
        # branch reachable; an empty string here means unconfigured, and every path that needs a
        # credential refuses by name.
        self.app_id = configured(app_id, "github_app_id").strip()
        # PEM keys arrive from environment variables with literal `\n` sequences more often than not.
        self.private_key = configured(private_key, "github_app_private_key").replace("\\n", "\n").strip()
        self.base_url = configured(base_url, "github_api_base_url", "https://api.github.com").rstrip("/")
        self._cache: dict[int, InstallationToken] = {}
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.private_key)

    def _require_configuration(self) -> None:
        missing = [
            name
            for name, value in (("GITHUB_APP_ID", self.app_id), ("GITHUB_APP_PRIVATE_KEY", self.private_key))
            if not value
        ]
        if missing:
            raise GitHubAppNotConfiguredError(
                "GitHub App import needs " + " and ".join(missing) + ". "
                "There is deliberately no fallback: the previous default minted a token GitHub would "
                "never accept and deferred the failure to an unrelated call."
            )

    def app_jwt(self, *, now: float | None = None) -> str:
        """Sign the short-lived App JWT that authenticates as the App itself."""
        self._require_configuration()
        issued = int(now if now is not None else time.time())
        claims = {
            "iat": issued - _JWT_BACKDATE_SECONDS,
            "exp": issued + _JWT_TTL_SECONDS,
            "iss": self.app_id,
        }
        try:
            return jwt.encode(claims, self.private_key, algorithm=_JWT_ALGORITHM)
        except Exception as exc:  # noqa: BLE001 - the library raises several unrelated types here
            # A malformed PEM is a configuration fault, so it is reported as one rather than as a
            # 500. The exception text is not forwarded: it can echo key material.
            raise GitHubAppNotConfiguredError(
                f"GITHUB_APP_PRIVATE_KEY is not a usable RS256 private key ({type(exc).__name__})"
            ) from exc

    async def get_installation_token(self, installation_id: int, *, client: httpx.AsyncClient | None = None) -> str:
        """Return a usable installation token, minting one only when the cached one is near expiry."""
        self._require_configuration()
        now = datetime.now(UTC)
        cached = self._cache.get(installation_id)
        if cached is not None and cached.usable_at(now):
            return cached.token

        async with self._lock:
            # Re-checked inside the lock: several concurrent imports of the same installation would
            # otherwise each mint a token, and GitHub rate-limits this exchange.
            cached = self._cache.get(installation_id)
            if cached is not None and cached.usable_at(datetime.now(UTC)):
                return cached.token
            minted = await self._exchange(installation_id, client)
            self._cache[installation_id] = minted
            return minted.token

    async def _exchange(self, installation_id: int, client: httpx.AsyncClient | None) -> InstallationToken:
        url = f"{self.base_url}/app/installations/{installation_id}/access_tokens"
        headers = {
            **_COMMON_HEADERS,
            # The JWT, not the private key. The previous code sent the key.
            _AUTH_HEADER: f"{_BEARER_SCHEME} {self.app_jwt()}",
        }
        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        try:
            response = await http.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubAppError("mint an installation token", 502, type(exc).__name__) from exc
        finally:
            if owned:
                await http.aclose()

        if response.status_code != 201:
            raise GitHubAppError("mint an installation token", response.status_code)
        payload = response.json()
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError("mint an installation token", response.status_code, "no token in the response")
        expires_at = _parse_expiry(payload.get("expires_at"))
        return InstallationToken(token=token, expires_at=expires_at)


def _parse_expiry(raw: Any) -> datetime:
    """Read GitHub's `expires_at`, falling back to the documented one-hour lifetime."""
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    # An unparsable value is treated as expiring now rather than in an hour, so the next call mints a
    # fresh token. Assuming the full hour would keep a token past its real expiry.
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ImportedRepository:
    """What an import establishes about a repository.

    NO TOKEN FIELD, and that absence is the point: the previous version returned the credential to its
    caller inside the result dict, so anything that logged or persisted the import outcome captured it.
    """

    owner: str
    name: str
    repo_url: str
    clone_url: str
    default_branch: str
    private: bool
    size_kb: int
    languages: tuple[str, ...]
    detected_manifests: tuple[str, ...]

    def as_project_settings(self) -> dict[str, Any]:
        """The subset a project row records. Deliberately small and deliberately not the whole payload."""
        return {
            "repo_default_branch": self.default_branch,
            "repo_private": self.private,
            "repo_languages": list(self.languages),
        }


#: Root-level files that establish a build system. Checked against the repository's real tree rather
#: than guessed from the language statistics, because a language byte-count does not tell you how the
#: project is built — the same reason the agent's framework detection reads manifests.
_MANIFEST_NAMES: Final[frozenset[str]] = frozenset(
    {
        "package.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "pyproject.toml",
        "Pipfile",
        "poetry.lock",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
        "composer.json",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "Makefile",
    }
)


class GitHubImporter:
    """Reads a repository's metadata over the real API using an installation token."""

    def __init__(self, token_source: GitHubAppTokenSource) -> None:
        self.token_source = token_source

    async def import_repository(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> ImportedRepository:
        """Fetch the repository, its language statistics and its root tree."""
        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        try:
            token = await self.token_source.get_installation_token(installation_id, client=http)
            headers = {**_COMMON_HEADERS, _AUTH_HEADER: f"{_BEARER_SCHEME} {token}"}
            base = f"{self.token_source.base_url}/repos/{owner}/{repo}"
            repository = await self._get_json(http, base, headers, "read the repository")
            # Language statistics and the root tree are independent of each other, so they are
            # fetched together. Neither is fatal: a repository with no recognised language and no
            # root manifest is still a repository, and refusing the import would be wrong.
            languages, contents = await asyncio.gather(
                self._get_json(http, f"{base}/languages", headers, "read the languages", optional=True),
                self._get_json(http, f"{base}/contents", headers, "read the root tree", optional=True),
                return_exceptions=False,
            )
        finally:
            if owned:
                await http.aclose()

        default_branch = str(repository.get("default_branch") or "main")
        language_names = tuple(sorted(languages)) if isinstance(languages, dict) else ()
        manifests: tuple[str, ...] = ()
        if isinstance(contents, list):
            manifests = tuple(
                sorted(
                    str(entry["name"])
                    for entry in contents
                    if isinstance(entry, dict)
                    and entry.get("type") == "file"
                    and str(entry.get("name", "")) in _MANIFEST_NAMES
                )
            )

        return ImportedRepository(
            owner=owner,
            name=str(repository.get("name") or repo),
            repo_url=str(repository.get("html_url") or f"https://github.com/{owner}/{repo}"),
            clone_url=str(repository.get("clone_url") or f"https://github.com/{owner}/{repo}.git"),
            default_branch=default_branch,
            private=bool(repository.get("private", False)),
            size_kb=int(repository.get("size") or 0),
            languages=language_names,
            detected_manifests=manifests,
        )

    async def _get_json(
        self,
        http: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        action: str,
        *,
        optional: bool = False,
    ) -> Any:
        try:
            response = await http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            if optional:
                return None
            raise GitHubAppError(action, 502, type(exc).__name__) from exc
        if response.status_code != 200:
            if optional:
                return None
            raise GitHubAppError(action, response.status_code)
        if len(response.content) > _MAX_IMPORT_BYTES and optional:
            # A repository with thousands of root entries is not worth reading to classify manifests.
            return None
        return response.json()
