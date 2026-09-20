# SPDX-License-Identifier: FSL-1.1-ALv2
"""Reading, writing and using one user's GitHub link.

WHY A SERVICE AND NOT SQL IN THE ROUTES. Three callers need the same three guarantees: every read is
predicated on `(user_id, tenant_id)`, no path returns the sealed bytes, and a token is refreshed before
use rather than after a 401. Written once here, those are properties of the type; written per route they
are properties of whoever wrote the route last.

THE ISOLATION RULE IS ENFORCED TWICE, deliberately. The WHERE clause carries both the user and the
tenant, AND the seal's additional authenticated data is the user id — so a row moved between users does
not open even if a predicate were dropped. One of those is a mistake away from being wrong; both being
wrong at once takes two.

THE TENANT PREDICATE IS `IS NOT DISTINCT FROM`, NOT `=`, and that is a defect fixed rather than a style
choice. D-35 defers enforced tenancy, so `principal.tenant_id` is `None` for a principal this
deployment issues today, and `tenant_id = NULL` is never true in SQL — a link written by such a
principal could be stored and then never read back, which is exactly what happened the first time this
was exercised against the running stack. `IS NOT DISTINCT FROM` treats NULL as a value, so a tenant-less
principal sees tenant-less rows and nothing else, which is the same rule `projects._tenant_clause`
states for the same reason. Two non-equal tenants still do not match, and a NULL does not match a real
tenant, so the isolation property is unchanged; what changed is that the deferred case works.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.github import GitHubAppError
from .github_link import (
    GitHubAccount,
    GitHubLinkError,
    GitHubLinkNotFoundError,
    GitHubOAuthClient,
    GitHubUserClient,
    Repository,
    UserToken,
    seal_token,
    unseal_token,
)

#: The columns a read path may select. `access_token_sealed` and `refresh_token_sealed` are ABSENT and
#: that is the point: a query that cannot name them cannot leak them into a response model by accident.
#: The two functions that do need the ciphertext name it explicitly and return a plaintext string that
#: never leaves this module's callers as a field.
PUBLIC_COLUMNS = (
    "user_id, tenant_id, github_login, github_user_id, github_avatar_url, scopes, "
    "credential_kind, access_token_expires_at, refresh_token_expires_at, created_at, updated_at, "
    "last_used_at, last_use_ok, last_use_detail"
)

#: The two ways a link can exist, and the reason the difference is recorded rather than inferred is in
#: migration `0020`. `OAUTH_APP` is the authorization-code flow; `PERSONAL_TOKEN` is a value the person
#: pasted, which reaches GitHub's UI not at all.
CREDENTIAL_OAUTH_APP = "oauth_app"
CREDENTIAL_PERSONAL_TOKEN = "personal_token"


@dataclass(frozen=True, slots=True)
class LinkRecord:
    """What a screen may know about a link. No credential field exists on this type."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    github_login: str
    github_user_id: int
    github_avatar_url: str
    scopes: str
    credential_kind: str
    access_token_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
    last_use_ok: bool | None
    last_use_detail: str

    @classmethod
    def from_row(cls, row: Any) -> LinkRecord:
        return cls(
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            github_login=str(row["github_login"]),
            github_user_id=int(row["github_user_id"]),
            github_avatar_url=str(row["github_avatar_url"] or ""),
            scopes=str(row["scopes"] or ""),
            credential_kind=str(row["credential_kind"] or CREDENTIAL_OAUTH_APP),
            access_token_expires_at=row["access_token_expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"],
            last_use_ok=row["last_use_ok"],
            last_use_detail=str(row["last_use_detail"] or ""),
        )


class GitHubLinkService:
    """One user's link: store it, read it, use it, remove it."""

    def __init__(
        self,
        *,
        oauth: GitHubOAuthClient,
        users: GitHubUserClient,
        link_key: bytes,
    ) -> None:
        self._oauth = oauth
        self._users = users
        self._key = link_key

    # ── the flow, as the routes see it ─────────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """Whether this deployment can link at all. Read by the status route, which reports it."""
        return self._oauth.configured

    def authorize(self, *, redirect_uri: str) -> tuple[str, str]:
        """The URL to visit and the single-use state that binds the eventual callback to a session."""
        state = self._oauth.new_state()
        return self._oauth.authorize_url(state=state, redirect_uri=redirect_uri), state

    async def complete(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        code: str,
        redirect_uri: str,
        client: httpx.AsyncClient | None = None,
    ) -> LinkRecord:
        """Exchange the code, read who it belongs to, and store the link.

        THE ACCOUNT IS READ BEFORE THE ROW IS WRITTEN, with the token that was just issued. Storing
        first and resolving the login later would leave a row that claims a link to an account nobody
        has verified — and the login is the only thing the screen shows, so it has to be GitHub's
        answer rather than the caller's claim.
        """
        token = await self._oauth.exchange_code(code, redirect_uri=redirect_uri, client=client)
        account = await self._users.account(token.access_token, client=client)
        return await self.store(session, user_id=user_id, tenant_id=tenant_id, token=token, account=account)

    async def link_with_token(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        token: str,
        client: httpx.AsyncClient | None = None,
    ) -> LinkRecord:
        """Link using a token the person pasted, with no browser redirect and no GitHub UI at all.

        WHY THIS EXISTS BESIDE THE AUTHORIZATION FLOW. The authorization-code flow necessarily sends the
        person to github.com — that is where consent is given — so they see GitHub's account chooser and
        authorize screen. Some operators do not want that: a shared workstation signed into several
        GitHub accounts makes the chooser a way to link the wrong one, and an air-gapped-ish deployment
        may have no registered App at all. This path needs neither an App nor a redirect.

        IT IS NOT A WEAKER LINK, and it is not a trust-me. The token is VERIFIED before anything is
        stored: `GET /user` is called with it, and the login and account id on the row are GitHub's
        answer rather than the caller's claim. A token GitHub refuses is refused here, so an unusable
        credential cannot be stored and discovered later as a listing that fails.

        WHAT IS RECORDED AND WHY. `credential_kind` is `personal_token`, because a disconnect cannot
        revoke one — GitHub has no API for an owner to revoke their own token — and the screen has to
        say so rather than claiming a revocation that did not happen. `expires_at` is `None`: a
        fine-grained token has an expiry GitHub does not report on this endpoint, and inventing one would
        make a live token look dead. When the token turns out to be expired, the listing fails with
        GitHub's own reason and the tri-state last-use line says so.
        """
        candidate = token.strip()
        if not candidate:
            raise GitHubLinkError("a token is required")
        account = await self._users.account(candidate, client=client)
        scopes = await self._users.scopes(candidate, client=client)
        return await self.store(
            session,
            user_id=user_id,
            tenant_id=tenant_id,
            token=UserToken(
                access_token=candidate,
                expires_at=None,
                refresh_token=None,
                refresh_expires_at=None,
                scopes=scopes,
            ),
            account=account,
            credential_kind=CREDENTIAL_PERSONAL_TOKEN,
        )

    # ── write ──────────────────────────────────────────────────────────────────────────────────────

    async def store(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        token: UserToken,
        account: GitHubAccount,
        credential_kind: str = CREDENTIAL_OAUTH_APP,
    ) -> LinkRecord:
        """Upsert the link. Re-connecting REPLACES rather than accumulating.

        One row per user is the primary key, so this is an upsert rather than a delete-then-insert:
        the two-step version has a window in which the user has no link, and a failure inside it
        leaves them disconnected having asked to connect.
        """
        sealed = seal_token(token.access_token, user_id=user_id, key=self._key)
        refresh_sealed = (
            seal_token(token.refresh_token, user_id=user_id, key=self._key) if token.refresh_token else None
        )
        result = await session.execute(
            text(
                "INSERT INTO github_account_links ("
                "  user_id, tenant_id, github_login, github_user_id, github_avatar_url, scopes, credential_kind,"
                "  access_token_sealed, access_token_expires_at, refresh_token_sealed,"
                "  refresh_token_expires_at, updated_at"
                ") VALUES ("
                "  :user_id, :tenant_id, :login, :github_user_id, :avatar, :scopes, :credential_kind,"
                "  :sealed, :expires_at, :refresh_sealed, :refresh_expires_at, now()"
                ") ON CONFLICT (user_id) DO UPDATE SET"
                "  tenant_id = EXCLUDED.tenant_id,"
                "  github_login = EXCLUDED.github_login,"
                "  github_user_id = EXCLUDED.github_user_id,"
                "  github_avatar_url = EXCLUDED.github_avatar_url,"
                "  scopes = EXCLUDED.scopes,"
                "  credential_kind = EXCLUDED.credential_kind,"
                "  access_token_sealed = EXCLUDED.access_token_sealed,"
                "  access_token_expires_at = EXCLUDED.access_token_expires_at,"
                "  refresh_token_sealed = EXCLUDED.refresh_token_sealed,"
                "  refresh_token_expires_at = EXCLUDED.refresh_token_expires_at,"
                "  updated_at = now(),"
                # Reset, because the evidence belongs to the credential that produced it. Keeping a
                # previous success against a new token would report a health that was never measured.
                "  last_used_at = NULL, last_use_ok = NULL, last_use_detail = ''"
                f" RETURNING {PUBLIC_COLUMNS}"
            ),
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "login": account.login,
                "github_user_id": account.account_id,
                "avatar": account.avatar_url,
                "scopes": token.scopes,
                "credential_kind": credential_kind,
                "sealed": sealed,
                "expires_at": token.expires_at,
                "refresh_sealed": refresh_sealed,
                "refresh_expires_at": token.refresh_expires_at,
            },
        )
        row = result.mappings().first()
        if row is None:  # pragma: no cover - an upsert with RETURNING either yields a row or raises
            raise GitHubLinkError("the GitHub link row could not be written")
        return LinkRecord.from_row(row)

    async def disconnect(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[str, bool, str]:
        """Delete the row and, where it is possible, revoke at GitHub.

        Returns `(login, revoked-at-GitHub, credential-kind)`. The kind is returned rather than left to
        the caller to look up, because the caller has to say something different for each: an App
        authorization has been revoked, and a pasted token has not and must be deleted on GitHub.

        THE LOCAL DELETE HAPPENS EITHER WAY, and the return value says what actually happened. A
        disconnect that refused because GitHub was unreachable would leave a user unable to do the safe
        thing during exactly the incident in which they want to.
        """
        token = await self._current_access_token(session, user_id=user_id, tenant_id=tenant_id)
        kind = await self._credential_kind(session, user_id=user_id, tenant_id=tenant_id)
        revoked = False
        if token is not None and kind == CREDENTIAL_OAUTH_APP:
            # ONLY AN APP AUTHORIZATION CAN BE REVOKED FROM HERE. GitHub's revocation endpoint
            # authenticates as the App and invalidates a token the App issued; a token the person
            # created belongs to them and there is no API for this server to delete it. Attempting it
            # anyway would answer 404 and be recorded as "not revoked", which is true but for the wrong
            # reason — so the attempt is not made and the caller is told plainly that they must delete
            # it on GitHub.
            revoked = await self._oauth.revoke(token, client=client)
        result = await session.execute(
            text(
                "DELETE FROM github_account_links "
                "WHERE user_id = :user_id AND tenant_id IS NOT DISTINCT FROM :tenant_id "
                "RETURNING github_login"
            ),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
        if row is None:
            raise GitHubLinkNotFoundError("this user has no GitHub link")
        return str(row["github_login"]), revoked, kind

    # ── read ───────────────────────────────────────────────────────────────────────────────────────

    async def read(self, session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID) -> LinkRecord | None:
        result = await session.execute(
            text(
                f"SELECT {PUBLIC_COLUMNS} FROM github_account_links "
                "WHERE user_id = :user_id AND tenant_id IS NOT DISTINCT FROM :tenant_id"
            ),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
        return LinkRecord.from_row(row) if row is not None else None

    # ── use ────────────────────────────────────────────────────────────────────────────────────────

    async def repositories(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[tuple[Repository, ...], bool]:
        """The real listing, from GitHub, for this user. Never a cache and never a stand-in.

        The outcome is recorded on the row — success or failure, with the reason — so the screen can
        distinguish "never used" from "failed" from "working" instead of showing a plausible list and
        letting a human act on it.
        """
        token = await self.usable_token(session, user_id=user_id, tenant_id=tenant_id, client=client)
        try:
            listing = await self._users.repositories(token, client=client)
        except Exception as exc:
            await self._record_use(session, user_id=user_id, ok=False, detail=_reason(exc))
            raise
        await self._record_use(
            session,
            user_id=user_id,
            ok=True,
            detail=f"listed {len(listing[0])} repository(ies)" + (" (truncated)" if listing[1] else ""),
        )
        return listing

    async def usable_token(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        client: httpx.AsyncClient | None = None,
    ) -> str:
        """The plaintext token, refreshed first if it is near expiry.

        REFRESHED BEFORE USE, not after a 401. A 401 from GitHub is indistinguishable at the call site
        from a revoked App or a deleted repository, so recovering from it there would mean guessing;
        the expiry GitHub stated is a fact already on the row.

        Returns a string rather than putting it on a record type. A credential that is a field is a
        credential that gets serialised by something that serialises records.
        """
        result = await session.execute(
            text(
                "SELECT access_token_sealed, access_token_expires_at, refresh_token_sealed, "
                "refresh_token_expires_at, scopes, github_login, github_user_id, github_avatar_url "
                "FROM github_account_links WHERE user_id = :user_id AND tenant_id IS NOT DISTINCT FROM :tenant_id"
            ),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
        if row is None:
            raise GitHubLinkNotFoundError("this user has no GitHub link")

        token = UserToken(
            access_token=unseal_token(row["access_token_sealed"], user_id=user_id, key=self._key),
            expires_at=row["access_token_expires_at"],
            refresh_token=(
                unseal_token(row["refresh_token_sealed"], user_id=user_id, key=self._key)
                if row["refresh_token_sealed"]
                else None
            ),
            refresh_expires_at=row["refresh_token_expires_at"],
            scopes=str(row["scopes"] or ""),
        )
        if token.usable_at(datetime.now(UTC)):
            return token.access_token
        if token.refresh_token is None:
            raise GitHubLinkError(
                "the GitHub token has expired and the App issued no refresh token, so the account "
                "must be connected again"
            )
        refreshed = await self._oauth.refresh(token.refresh_token, client=client)
        await self.store(
            session,
            user_id=user_id,
            tenant_id=tenant_id,
            token=refreshed,
            account=GitHubAccount(
                login=str(row["github_login"]),
                account_id=int(row["github_user_id"]),
                avatar_url=str(row["github_avatar_url"] or ""),
            ),
        )
        return refreshed.access_token

    async def _credential_kind(self, session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
        """Which of the two kinds this link is. Defaults to the App kind for a pre-`0020` row."""
        result = await session.execute(
            text(
                "SELECT credential_kind FROM github_account_links "
                "WHERE user_id = :user_id AND tenant_id IS NOT DISTINCT FROM :tenant_id"
            ),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
        return str(row["credential_kind"]) if row is not None else CREDENTIAL_OAUTH_APP

    async def _current_access_token(
        self, session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> str | None:
        """The stored token without refreshing it. Used only by revocation, which wants the old one."""
        result = await session.execute(
            text(
                "SELECT access_token_sealed FROM github_account_links "
                "WHERE user_id = :user_id AND tenant_id IS NOT DISTINCT FROM :tenant_id"
            ),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()
        if row is None:
            return None
        try:
            return unseal_token(row["access_token_sealed"], user_id=user_id, key=self._key)
        except GitHubLinkError:
            # A row that cannot be opened still has to be deletable, otherwise a pepper rotation
            # leaves users unable to disconnect. The revocation is skipped and the delete proceeds.
            return None

    async def _record_use(self, session: AsyncSession, *, user_id: uuid.UUID, ok: bool, detail: str) -> None:
        await session.execute(
            text(
                "UPDATE github_account_links SET last_used_at = now(), last_use_ok = :ok, "
                "last_use_detail = :detail WHERE user_id = :user_id"
            ),
            {"user_id": user_id, "ok": ok, "detail": detail[:1024]},
        )


def _reason(exc: BaseException) -> str:
    """A failure description safe to store: the type, plus the message an error type already sanitised.

    `GitHubAppError` is built to carry a status and never a response body, so its text is safe. Anything
    else contributes its type name only — an arbitrary exception's message can contain a URL, and a URL
    can contain a token.
    """
    if isinstance(exc, GitHubAppError | GitHubLinkError | GitHubLinkNotFoundError):
        return str(exc)[:1024]
    return type(exc).__name__
