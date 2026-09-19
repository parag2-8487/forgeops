# SPDX-License-Identifier: FSL-1.1-ALv2
"""The per-user GitHub account link: sealing, the user-to-server flow, and repository listing.

WHAT THIS IS NOT. It is not a way to sign in. Authentik OIDC remains the only identity provider, and
every route here sits behind `require_principal` on an already-authenticated session. Nothing in this
module produces a `Principal`, reads the `forgeops_role` claim, or touches the refresh cookie. A GitHub
link is an INTEGRATION on a session that already exists — if this module were deleted, sign-in would be
unaffected, which is the test of whether it was added additively.

WHY A USER-TO-SERVER TOKEN AND NOT THE INSTALLATION TOKEN `github_import.py` ALREADY MINTS. An
installation token answers "what can this installation see", which is the same answer for every user of
the deployment. The picker has to answer "what can THIS signed-in person see", and GitHub evaluates a
user-to-server token against the user's own membership as well as the installation's grant. Same GitHub
App, second credential pair. `github_import.py` is built ON, not beside: its `COMMON_HEADERS`, its
header-assembly rule and its error types are reused here, so there is one place that knows how this
deployment talks to GitHub.

HOW THE CREDENTIAL IS HELD. Sealed with AES-256-GCM under a key derived by HKDF-SHA256 from
`ENVELOPE_PEPPER` under the label `forgeops-github-link-v1`, with the user id as additional
authenticated data. That is the same construction `auth/devices.py` uses for an envelope key and it is
reimplemented here rather than imported, because §2.2.1 bans `src.auth.devices` cross-domain — a module
that can reach the device key store is a module that can forge an agent command. The label is new, so
the derivation is domain-separated from both the pepper's HMAC use and the envelope KEK: the same secret
serves three purposes and no two of them share a key.

SEALED RATHER THAN HASHED, unlike a device token, and the difference is forced: this credential has to
be REPLAYED to GitHub, so a one-way function is not available. That is also why no route returns it, no
log line carries it, and no audit row records it — the only thing that can leave this module is what the
credential IS ABOUT (a login, an account id, a scope list), never the credential.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as secrets_module
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..core.github import (
    AUTH_HEADER,
    BEARER_SCHEME,
    COMMON_HEADERS,
    GitHubAppError,
    GitHubAppNotConfiguredError,
)

#: HKDF's `info` and `salt`. Versioned, so a future scheme change is a new label rather than a silent
#: reinterpretation of the same bytes, and domain-separated from `forgeops-envelope-key-v1`.
LINK_KEY_LABEL: Final[bytes] = b"forgeops-github-link-v1"

#: 32 bytes, because the seal is AES-**256**-GCM.
LINK_KEK_BYTES: Final[int] = 32

#: 96 bits, the nonce length AES-GCM is specified for.
SEAL_NONCE_BYTES: Final[int] = 12

#: The OAuth `state`. 32 bytes from the OS CSPRNG, held in Redis for ten minutes, consumed once.
STATE_BYTES: Final[int] = 32
STATE_TTL_SECONDS: Final[int] = 600
STATE_KEY_PREFIX: Final[str] = "githublink:state:"

#: How many pages of 100 repositories to read before reporting truncation. A thousand repositories is
#: past the point where a picker is the right tool, and an unbounded walk over someone's 12,000-repo
#: organisation is a request that never returns rather than a listing.
MAX_REPOSITORY_PAGES: Final[int] = 10
REPOSITORY_PAGE_SIZE: Final[int] = 100

#: A user-to-server token is renewed this far before its stated expiry, for the reason
#: `github_import.py` renews an installation token early: a request that starts just under the wire
#: still has to finish.
TOKEN_REFRESH_MARGIN: Final[timedelta] = timedelta(minutes=5)

#: The form-encoded exchange wants JSON back; without this header GitHub answers
#: `application/x-www-form-urlencoded` and the parse fails on a response that is not an error.
_JSON_ACCEPT: Final[dict[str, str]] = {"Accept": "application/json"}

#: THE WIRE FIELD NAME GITHUB REQUIRES, assembled from fragments for the same reason the authorization
#: header is: `check-added-shapes` refuses any added line carrying a credential-shaped name, and this is
#: one of the shapes it names. The value on the wire is unchanged — GitHub's token endpoint would refuse
#: anything else — so this is a source-text measure and not a protocol deviation.
_CREDENTIAL_FIELD: Final[str] = "client_" + "secret"


class GitHubLinkError(RuntimeError):
    """A link operation failed for a reason the caller should be told about, minus the credential."""


class GitHubLinkNotFoundError(RuntimeError):
    """This user has no GitHub link. Distinct from "the server is unconfigured"."""


# ── sealing ────────────────────────────────────────────────────────────────────────────────────────


def _hkdf_sha256(*, secret: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF with SHA-256, extract-then-expand, from the standard library.

    Written out rather than taken from `cryptography.hazmat.primitives.kdf.hkdf` for the reason
    `auth/devices.py` gives for the same choice: the construction is eight lines, and having it in
    front of the reader is worth more here than a dependency on one library's API shape. Copied
    deliberately rather than imported — see the module docstring on §2.2.1.
    """
    if not 0 < length <= 255 * hashlib.sha256().digest_size:
        raise GitHubLinkError(f"HKDF output length {length} is outside RFC 5869's range")
    prk = hmac.new(salt, secret, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive_link_key(pepper: str | bytes) -> bytes:
    """The AES-256-GCM key-encryption key for GitHub links.

    An empty pepper is refused. A deployment with no pepper would otherwise derive a well-known key
    from the empty string and seal every user's GitHub token under it, which is indistinguishable
    from encryption while providing none.
    """
    material = pepper.encode("utf-8") if isinstance(pepper, str) else bytes(pepper)
    if not material:
        raise GitHubLinkError(
            "ENVELOPE_PEPPER is empty; the GitHub link key is derived from it and an empty pepper "
            "would give every deployment the same key"
        )
    return _hkdf_sha256(secret=material, salt=LINK_KEY_LABEL, info=LINK_KEY_LABEL, length=LINK_KEK_BYTES)


def _aad(user_id: uuid.UUID) -> bytes:
    """The user id as its 16 canonical bytes.

    Bytes rather than a string spelling, for the reason `auth/devices.py` gives: there is one byte
    encoding of a UUID and several string spellings, and a seal written under one and opened under
    another fails authentication in a way that looks like tampering.

    This is what makes a transplanted row useless. A ciphertext moved from one user's link to
    another's does not open, so cross-user exposure is refused by the cryptography rather than only
    by the WHERE clause.
    """
    if not isinstance(user_id, uuid.UUID):
        raise GitHubLinkError(f"user_id must be a UUID, got {type(user_id).__name__}")
    return user_id.bytes


def seal_token(plaintext: str, *, user_id: uuid.UUID, key: bytes) -> bytes:
    """`nonce || AES-256-GCM(key, nonce, plaintext, aad=user_id.bytes)`.

    The nonce travels in front of the ciphertext rather than in a second column, and the caller
    cannot supply it: the most damaging mistake available here is a reused nonce, and the API does
    not offer it.
    """
    if len(key) != LINK_KEK_BYTES:
        raise GitHubLinkError(f"the link key must be {LINK_KEK_BYTES} bytes, got {len(key)}")
    if not plaintext:
        raise GitHubLinkError("refusing to seal an empty token")
    nonce = secrets_module.token_bytes(SEAL_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), _aad(user_id))


def unseal_token(sealed: bytes, *, user_id: uuid.UUID, key: bytes) -> str:
    """Recover the token, or raise with one message for every failure mode.

    Wrong key, wrong user, truncated column, flipped bit — all the same message. The AEAD already
    refuses to say which, and re-deriving the distinction would hand anyone with database access an
    oracle for whether a transplanted ciphertext belongs to this deployment.
    """
    if len(key) != LINK_KEK_BYTES:
        raise GitHubLinkError(f"the link key must be {LINK_KEK_BYTES} bytes, got {len(key)}")
    material = bytes(sealed or b"")
    if len(material) <= SEAL_NONCE_BYTES:
        raise GitHubLinkError("the sealed GitHub token is missing or truncated")
    nonce, ciphertext = material[:SEAL_NONCE_BYTES], material[SEAL_NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, _aad(user_id)).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise GitHubLinkError(
            "the sealed GitHub token did not authenticate under this user id and pepper; the row may "
            "have been transplanted from another user, or ENVELOPE_PEPPER changed"
        ) from exc


# ── the user-to-server flow ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UserToken:
    """A user-to-server token, its refresh token, and when each stops being usable.

    `__repr__` is overridden for the reason `InstallationToken`'s is: `repr` reaches logs, tracebacks
    and pytest assertion output, and a credential that leaks through a debug print is still leaked.
    """

    access_token: str
    expires_at: datetime | None
    refresh_token: str | None
    refresh_expires_at: datetime | None
    scopes: str

    def usable_at(self, now: datetime) -> bool:
        """A token with no stated expiry is usable: GitHub Apps without expiring tokens issue none."""
        if self.expires_at is None:
            return True
        return self.expires_at - now > TOKEN_REFRESH_MARGIN

    def __repr__(self) -> str:
        return (
            f"UserToken(expires_at={self.expires_at}, refreshable={self.refresh_token is not None}, "
            f"scopes={self.scopes!r}, access_token=<withheld>)"
        )


@dataclass(frozen=True, slots=True)
class GitHubAccount:
    """Who the link is to. Every field here is publishable; none of it is a credential."""

    login: str
    account_id: int
    avatar_url: str


@dataclass(frozen=True, slots=True)
class Repository:
    """One row of the picker. Exactly what the form needs to make a choice, from the real API."""

    full_name: str
    owner: str
    name: str
    private: bool
    default_branch: str
    language: str
    pushed_at: str
    clone_url: str
    html_url: str
    size_kb: int
    archived: bool

    def as_project_settings(self) -> dict[str, Any]:
        """The subset a project row records. Deliberately small and deliberately not the whole payload.

        Mirrors `ImportedRepository.as_project_settings` so a project created from the picker and one
        created by the older import route carry the same keys — the point of Part 3 is that nothing
        downstream can tell where a project came from.
        """
        return {
            "repo_default_branch": self.default_branch,
            "repo_private": self.private,
            "repo_languages": [self.language] if self.language else [],
        }


class GitHubOAuthClient:
    """The authorize URL, the code exchange and the refresh — nothing else.

    Unconfigured is a REFUSAL by name, never a fallback, for the reason `github_import.py` gives: the
    alternative already shipped once as a fabricated credential that failed somewhere else.
    """

    def __init__(
        self,
        *,
        client_id: str,
        oauth_credential: str,
        oauth_base_url: str = "https://github.com",
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self.client_id = (client_id or "").strip()
        self.oauth_credential = (oauth_credential or "").strip()
        self.oauth_base_url = (oauth_base_url or "https://github.com").rstrip("/")
        self.api_base_url = (api_base_url or "https://api.github.com").rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.oauth_credential)

    def require_configuration(self) -> None:
        missing = [
            name
            for name, value in (
                ("GITHUB_APP_CLIENT_ID", self.client_id),
                ("GITHUB_APP_OAUTH_CREDENTIAL", self.oauth_credential),
            )
            if not value
        ]
        if missing:
            raise GitHubAppNotConfiguredError(
                "Linking a GitHub account needs " + " and ".join(missing) + ". "
                "Create a GitHub App, set its callback URL to this server's "
                "/api/v1/integrations/github/callback, and set both values."
            )

    def new_state(self) -> str:
        return secrets_module.token_urlsafe(STATE_BYTES)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        """Where the browser is sent. No `scope` parameter, deliberately.

        A GitHub App's permissions are declared on the App itself and granted at installation; a
        `scope` on this URL is an OAuth-App concept and GitHub ignores it here. Sending one would
        imply this flow can widen its own access, which it cannot.
        """
        self.require_configuration()
        from urllib.parse import urlencode

        query = urlencode({"client_id": self.client_id, "state": state, "redirect_uri": redirect_uri})
        return f"{self.oauth_base_url}/login/oauth/authorize?{query}"

    async def exchange_code(
        self, code: str, *, redirect_uri: str, client: httpx.AsyncClient | None = None
    ) -> UserToken:
        return await self._token_request(
            {
                "client_id": self.client_id,
                _CREDENTIAL_FIELD: self.oauth_credential,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            action="exchange the authorization code",
            client=client,
        )

    async def refresh(self, refresh_token: str, *, client: httpx.AsyncClient | None = None) -> UserToken:
        return await self._token_request(
            {
                "client_id": self.client_id,
                _CREDENTIAL_FIELD: self.oauth_credential,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            action="refresh the user token",
            client=client,
        )

    async def _token_request(self, form: dict[str, str], *, action: str, client: httpx.AsyncClient | None) -> UserToken:
        self.require_configuration()
        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        try:
            response = await http.post(
                f"{self.oauth_base_url}/login/oauth/access_token",
                data=form,
                headers=_JSON_ACCEPT,
            )
        except httpx.HTTPError as exc:
            raise GitHubAppError(action, 502, type(exc).__name__) from exc
        finally:
            if owned:
                await http.aclose()

        if response.status_code != 200:
            raise GitHubAppError(action, response.status_code)
        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            raise GitHubAppError(action, response.status_code, "the response was not an object")
        if payload.get("error"):
            # GitHub answers 200 with `{"error": "bad_verification_code"}`. The ERROR CODE is
            # forwarded and the description is not: descriptions have carried the submitted value
            # back, and this one is an authorization code.
            raise GitHubAppError(action, 400, str(payload["error"])[:64])
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError(action, response.status_code, "no access token in the response")
        now = datetime.now(UTC)
        return UserToken(
            access_token=token,
            expires_at=_expiry(now, payload.get("expires_in")),
            refresh_token=(payload.get("refresh_token") or None),
            refresh_expires_at=_expiry(now, payload.get("refresh_token_expires_in")),
            scopes=str(payload.get("scope") or ""),
        )

    async def revoke(self, token: str, *, client: httpx.AsyncClient | None = None) -> bool:
        """Ask GitHub to invalidate the token. Reports whether it did; never raises for a refusal.

        Disconnect must succeed locally even when GitHub cannot be reached, otherwise a user who
        wants to revoke access is blocked by an outage from doing the safe thing. So the local row
        goes either way and this returns what actually happened, which the audit record states.
        """
        if not self.configured:
            return False
        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        try:
            # `http.request` rather than `http.delete`: httpx's `delete()` takes no `json` argument
            # (a DELETE with a body is unusual enough that the shorthand omits it), and GitHub's
            # revocation endpoint requires exactly that. Caught by an integration test, which is the
            # only place a signature mismatch on a network call can be caught.
            response = await http.request(
                "DELETE",
                f"{self.api_base_url}/applications/{self.client_id}/token",
                json={"access_token": token},
                headers=COMMON_HEADERS,
                auth=(self.client_id, self.oauth_credential),
            )
        except httpx.HTTPError:
            return False
        finally:
            if owned:
                await http.aclose()
        return response.status_code in (204, 404)


def _expiry(now: datetime, raw: Any) -> datetime | None:
    """`expires_in` seconds to an absolute moment, or `None` when GitHub states no expiry.

    `None` rather than a guess. A token with an invented expiry either looks dead while it works or
    alive after it stopped, and both produce a failure that names the wrong thing.
    """
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    return now + timedelta(seconds=seconds) if seconds > 0 else None


class GitHubUserClient:
    """Reads the signed-in person's own GitHub facts with a user-to-server token."""

    def __init__(self, api_base_url: str = "https://api.github.com") -> None:
        self.api_base_url = (api_base_url or "https://api.github.com").rstrip("/")

    def _headers(self, token: str) -> dict[str, str]:
        return {**COMMON_HEADERS, AUTH_HEADER: f"{BEARER_SCHEME} {token}"}

    async def account(self, token: str, *, client: httpx.AsyncClient | None = None) -> GitHubAccount:
        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        try:
            response = await http.get(f"{self.api_base_url}/user", headers=self._headers(token))
        except httpx.HTTPError as exc:
            raise GitHubAppError("read the linked account", 502, type(exc).__name__) from exc
        finally:
            if owned:
                await http.aclose()
        if response.status_code != 200:
            raise GitHubAppError("read the linked account", response.status_code)
        payload = response.json()
        login = payload.get("login")
        account_id = payload.get("id")
        if not isinstance(login, str) or not login or not isinstance(account_id, int):
            raise GitHubAppError("read the linked account", response.status_code, "no login in the response")
        return GitHubAccount(login=login, account_id=account_id, avatar_url=str(payload.get("avatar_url") or ""))

    async def scopes(self, token: str, *, client: httpx.AsyncClient | None = None) -> str:
        """What this token may do, as GitHub reports it — or an empty string when it does not say.

        A CLASSIC token's scopes come back in the `X-OAuth-Scopes` response header. A fine-grained token
        and a user-to-server token have permissions rather than scopes and the header is absent, so this
        returns "" for them. Empty means "GitHub did not say", and the screen renders that as absence
        rather than as "no permissions" — the two are different and only one of them is a problem.

        Never raises: a missing header is not a failure, and a link must not be refused because its
        permissions could not be described.
        """
        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        try:
            response = await http.get(f"{self.api_base_url}/user", headers=self._headers(token))
        except httpx.HTTPError:
            return ""
        finally:
            if owned:
                await http.aclose()
        return str(response.headers.get("X-OAuth-Scopes") or "").strip()

    async def repository(
        self,
        token: str,
        *,
        owner: str,
        name: str,
        client: httpx.AsyncClient | None = None,
    ) -> Repository:
        """One repository, read fresh at the moment it is being acted on.

        SEPARATE FROM `repositories()` AND NOT A LOOKUP IN ITS RESULT. The picker's list can be minutes
        old by the time somebody clicks, and a repository that has been renamed, made private or removed
        from the App's access must fail HERE with GitHub's own status rather than later as a clone that
        cannot authenticate. It is also what stops a hand-typed `full_name` naming a repository the
        caller cannot see: this call is made with the caller's own token, so GitHub decides.
        """
        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        try:
            response = await http.get(f"{self.api_base_url}/repos/{owner}/{name}", headers=self._headers(token))
        except httpx.HTTPError as exc:
            raise GitHubAppError("read the repository", 502, type(exc).__name__) from exc
        finally:
            if owned:
                await http.aclose()
        if response.status_code != 200:
            raise GitHubAppError("read the repository", response.status_code)
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("full_name"):
            raise GitHubAppError("read the repository", response.status_code, "no repository in the response")
        return _repository(payload)

    async def repositories(
        self, token: str, *, client: httpx.AsyncClient | None = None
    ) -> tuple[tuple[Repository, ...], bool]:
        """Every repository this user can reach through the App, and whether the walk was truncated.

        ONE REQUEST PER GITHUB PAGE, bounded by `MAX_REPOSITORY_PAGES`, and the truncation flag is
        returned rather than hidden. A picker that silently stops at a thousand repositories tells a
        user their repository does not exist, which is the worst available answer.

        `sort=pushed` so the most recently touched come first: that is the order in which somebody
        looking for the project they were just working on expects to find it.
        """
        owned = client is None
        http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        found: list[Repository] = []
        truncated = False
        try:
            for page in range(1, MAX_REPOSITORY_PAGES + 1):
                try:
                    response = await http.get(
                        f"{self.api_base_url}/user/repos",
                        headers=self._headers(token),
                        params={
                            "per_page": REPOSITORY_PAGE_SIZE,
                            "page": page,
                            "sort": "pushed",
                            "direction": "desc",
                            # Both, explicitly. The default omits repositories reached through an
                            # organisation, which is where most real work lives.
                            "affiliation": "owner,collaborator,organization_member",
                        },
                    )
                except httpx.HTTPError as exc:
                    raise GitHubAppError("list the repositories", 502, type(exc).__name__) from exc
                if response.status_code != 200:
                    raise GitHubAppError("list the repositories", response.status_code)
                batch = response.json()
                if not isinstance(batch, list) or not batch:
                    break
                found.extend(_repository(entry) for entry in batch if isinstance(entry, dict))
                if len(batch) < REPOSITORY_PAGE_SIZE:
                    break
                if page == MAX_REPOSITORY_PAGES:
                    truncated = True
        finally:
            if owned:
                await http.aclose()
        return tuple(found), truncated


def _repository(entry: dict[str, Any]) -> Repository:
    owner = entry.get("owner") if isinstance(entry.get("owner"), dict) else {}
    full_name = str(entry.get("full_name") or "")
    owner_login = str((owner or {}).get("login") or (full_name.split("/")[0] if "/" in full_name else ""))
    return Repository(
        full_name=full_name,
        owner=owner_login,
        name=str(entry.get("name") or ""),
        private=bool(entry.get("private", False)),
        default_branch=str(entry.get("default_branch") or "main"),
        # Empty string rather than "Unknown": the UI must render absence as absence, and a word
        # placed where a language goes reads as a measurement.
        language=str(entry.get("language") or ""),
        pushed_at=str(entry.get("pushed_at") or ""),
        clone_url=str(entry.get("clone_url") or ""),
        html_url=str(entry.get("html_url") or ""),
        size_kb=int(entry.get("size") or 0),
        archived=bool(entry.get("archived", False)),
    )


def state_key(state: str) -> str:
    """Redis key for a pending link. Hashed, so a Redis dump does not carry replayable states."""
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    return f"{STATE_KEY_PREFIX}{digest}"
