# SPDX-License-Identifier: FSL-1.1-ALv2
"""The GitHub account-link HTTP surface.

ADDITIVE, AND THAT IS ASSERTABLE. Every route here carries `require_principal` through the router's
`dependencies`, exactly as `projects` and `approvals` do, so the deny-by-default property holds the
moment a route is declared. There is no sign-in route, no cookie written, no token minted for this
platform, and no `Principal` produced: `/connect` returns a URL for an ALREADY-AUTHENTICATED caller to
visit, and `/callback` attaches what comes back to the session that started it. Delete this module and
Authentik sign-in is unchanged — which is the test of additive.

WHERE THE CALLBACK'S AUTHENTICATION COMES FROM, since a browser redirect from GitHub carries no bearer
token. The `state` is minted per user, held in Redis under a HASH of itself for ten minutes, and
consumed once with `GETDEL` — the same construction `auth/routes.py` uses for the OIDC PKCE state, and
for the same reason: a two-step read-then-delete lets a replayed `state` be accepted twice. The record
in Redis carries the user id and tenant id, so the callback binds the credential to the session that
asked for it rather than to whoever happens to arrive. `/callback` is therefore the ONE route here
outside the router's principal dependency, and it is authenticated by a single-use secret it issued
itself. It is registered on a separate router for the same reason the device routes are: a route whose
authentication `require_principal` cannot express must not sit under it and look as though it does.

WHY THE CONNECT FLOW LIVES IN SETTINGS AND IS ALSO REACHABLE FROM PROJECT CREATION. A link is a
property of the person, not of a project, so its home is `/settings/integrations` where it can be
inspected and revoked without a project in hand. It is ALSO surfaced on the create-project form,
because that is the one place where the absence of a link blocks the thing the user is trying to do,
and sending them to a settings page to come back afterwards loses the form.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from typing import Annotated, Any, Final

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit.writer import AuditDraft
from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import problem
from ..core.github import GitHubAppError, GitHubAppNotConfiguredError
from .github_link import (
    STATE_TTL_SECONDS,
    GitHubLinkError,
    GitHubLinkNotFoundError,
    Repository,
    state_key,
)
from .service import GitHubLinkService, LinkRecord

router = APIRouter(
    prefix="/api/v1/integrations",
    tags=["integrations"],
    dependencies=[Depends(require_principal)],
)

#: The callback, on its own router for the reason the module docstring gives: it is authenticated by a
#: single-use state it minted, which `require_principal` cannot express.
callback_router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])

#: Page size ceiling, mirroring the approvals and projects surfaces so one concept has one bound.
MAX_PAGE_SIZE: Final[int] = 100
DEFAULT_PAGE_SIZE: Final[int] = 25


# ── wire shapes ────────────────────────────────────────────────────────────────────────────────────


class GitHubLinkStatus(BaseModel):
    """What the screen is told. There is deliberately no token field on this model.

    `configured` and `connected` are SEPARATE booleans because they have different remedies: an
    operator fixes the first and a user fixes the second, and collapsing them into one "unavailable"
    would send every fresh install's user looking for a connect button that cannot work yet.
    """

    configured: bool
    connected: bool
    #: What to set, when `configured` is false. Present rather than left to the UI to invent, so one
    #: place knows the variable names.
    configuration_hint: str = ""
    login: str | None = None
    avatar_url: str | None = None
    scopes: list[str] = Field(default_factory=list)
    connected_at: str | None = None
    #: Tri-state, like the pairing screen's heartbeat: `None` means the link has never been used,
    #: which is not the same as failing and must not render as it.
    last_use_ok: bool | None = None
    last_used_at: str | None = None
    last_use_detail: str = ""
    access_token_expires_at: str | None = None


class ConnectResponse(BaseModel):
    """Where to send the browser. The `state` is NOT returned; it lives in Redis and in the URL."""

    authorize_url: str
    expires_in_seconds: int


class RepositoryItem(BaseModel):
    full_name: str
    owner: str
    name: str
    private: bool
    default_branch: str
    #: Empty string means GitHub reported no primary language. The UI must render that as absence.
    language: str = ""
    pushed_at: str = ""
    clone_url: str = ""
    html_url: str = ""
    size_kb: int = 0
    archived: bool = False


class RepositoryPage(BaseModel):
    """One page of the picker, plus the two facts that keep it honest.

    `total` is the number of repositories MATCHING the query across everything fetched, so a client can
    say "12 of 340" rather than implying the page is the whole. `truncated` says the walk hit its bound
    — without it, a user whose repository is past the bound is told it does not exist.
    """

    items: list[RepositoryItem]
    total: int
    page: int
    per_page: int
    truncated: bool


class DisconnectResponse(BaseModel):
    login: str
    #: Whether GitHub confirmed the token is dead. False means the local link is gone and the token
    #: may still be live at GitHub, which is a different fact and is stated rather than implied.
    revoked_at_github: bool


# ── composition ────────────────────────────────────────────────────────────────────────────────────


def _service(request: Request) -> GitHubLinkService:
    """The composed service, or a loud failure.

    A `RuntimeError` rather than a 503, following `require_principal`'s reasoning and the approvals
    surface's: an unassembled service is a composition error in the app factory, not a fact about the
    caller, and reporting it as an outage would let a broken deployment look like a working one
    refusing work.
    """
    service = getattr(request.app.state, "github_link_service", None)
    if service is None:
        raise RuntimeError(
            "app.state.github_link_service is not composed; the integrations surface depends on it. "
            "create_app() must build it in the lifespan."
        )
    return service


def _writer(request: Request) -> Any:
    writer = getattr(request.app.state, "audit_writer", None)
    if writer is None:
        raise RuntimeError("app.state.audit_writer is not composed; every mutation must be auditable")
    return writer


def _callback_url(request: Request) -> str:
    """The redirect URI, derived from the request rather than configured.

    One fewer setting to get wrong, and it cannot disagree with the URL the browser actually reached.
    GitHub compares this against the App's registered callback, so a mismatch is reported by GitHub
    with the value it received — which is the error a human can act on.
    """
    return str(request.url_for("github_link_callback"))


def _unconfigured(exc: GitHubAppNotConfiguredError | None = None) -> Any:
    detail = (
        str(exc)
        if exc is not None
        else (
            "This server has no GitHub App client credentials configured, so a GitHub account cannot "
            "be linked. Set GITHUB_APP_CLIENT_ID and GITHUB_APP_OAUTH_CREDENTIAL, and register this "
            "server's /api/v1/integrations/github/callback as the App's callback URL."
        )
    )
    return problem("github-link-unconfigured", detail=detail)


# ── routes ─────────────────────────────────────────────────────────────────────────────────────────


@router.get("/github", response_model=GitHubLinkStatus, summary="Whether this user has linked GitHub")
async def read_github_link(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GitHubLinkStatus:
    """Report the link, or its absence, or the server's unconfiguration — as three distinct facts.

    A 200 with `configured: false` rather than a 503, because this is the route a screen loads to
    decide what to render. Answering 503 here would make "the operator has not set this up" arrive as
    an error banner instead of as instructions, which is the state of every fresh install.
    """
    service = _service(request)
    configured = service.is_configured()
    record = await service.read(session, user_id=principal.user_id, tenant_id=principal.tenant_id)
    return _status(configured=configured, record=record)


def _status(*, configured: bool, record: LinkRecord | None) -> GitHubLinkStatus:
    if record is None:
        return GitHubLinkStatus(
            configured=configured,
            connected=False,
            configuration_hint="" if configured else _CONFIGURATION_HINT,
        )
    return GitHubLinkStatus(
        configured=configured,
        connected=True,
        login=record.github_login,
        avatar_url=record.github_avatar_url or None,
        scopes=[scope for scope in record.scopes.replace(",", " ").split() if scope],
        connected_at=record.created_at.isoformat(),
        last_use_ok=record.last_use_ok,
        last_used_at=record.last_used_at.isoformat() if record.last_used_at else None,
        last_use_detail=record.last_use_detail,
        access_token_expires_at=(
            record.access_token_expires_at.isoformat() if record.access_token_expires_at else None
        ),
    )


_CONFIGURATION_HINT: Final[str] = (
    "Set GITHUB_APP_CLIENT_ID and GITHUB_APP_OAUTH_CREDENTIAL (and GITHUB_APP_ID with "
    "GITHUB_APP_PRIVATE_KEY for cloning), then register this server's "
    "/api/v1/integrations/github/callback as the GitHub App's callback URL."
)


@router.post("/github/connect", response_model=ConnectResponse, summary="Begin linking a GitHub account")
async def begin_github_link(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
) -> ConnectResponse:
    """Mint a single-use state bound to this user and return the URL to visit.

    No redirect response, deliberately: the caller is a `fetch` from an application that holds its
    access token in memory, and a 302 would be followed by the fetch rather than by the browser. The
    client navigates.
    """
    service = _service(request)
    try:
        authorize_url, state = service.authorize(redirect_uri=_callback_url(request))
    except GitHubAppNotConfiguredError as exc:
        raise _unconfigured(exc) from exc

    await request.app.state.redis.set(
        state_key(state),
        json.dumps({"user_id": str(principal.user_id), "tenant_id": str(principal.tenant_id)}),
        ex=STATE_TTL_SECONDS,
    )
    return ConnectResponse(authorize_url=authorize_url, expires_in_seconds=STATE_TTL_SECONDS)


@callback_router.get(
    "/github/callback",
    name="github_link_callback",
    response_model=GitHubLinkStatus,
    summary="Finish linking a GitHub account (browser redirect target)",
)
async def github_link_callback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    code: str = Query(..., min_length=1, max_length=512),
    state: str = Query(..., min_length=1, max_length=512),
) -> GitHubLinkStatus:
    """Exchange the code for a user-to-server token and attach it to the session that asked.

    AUTHENTICATED BY THE STATE, not by a bearer: see the module docstring. The state is consumed
    atomically, so a replay finds nothing and is refused with the same message as an expired one —
    there is no way to tell from outside whether a state was wrong, used or stale, which is what stops
    this being an oracle.
    """
    raw = await request.app.state.redis.getdel(state_key(state))
    if raw is None:
        raise problem(
            "github-link-failed",
            detail=(
                "This GitHub authorization could not be matched to a pending request. Start the "
                "connection again from the integrations page; a link request is single-use and "
                "expires after ten minutes."
            ),
        )
    try:
        pending = json.loads(raw)
        user_id = uuid.UUID(str(pending["user_id"]))
        tenant_id = uuid.UUID(str(pending["tenant_id"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise problem("github-link-failed", detail="the pending link record was unreadable") from exc

    service = _service(request)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        try:
            record = await service.complete(
                session,
                user_id=user_id,
                tenant_id=tenant_id,
                code=code,
                redirect_uri=_callback_url(request),
                client=client,
            )
        except GitHubAppNotConfiguredError as exc:
            raise _unconfigured(exc) from exc
        except (GitHubAppError, GitHubLinkError) as exc:
            raise problem("github-link-failed", detail=str(exc)) from exc

    await _writer(request).append(
        session,
        AuditDraft(
            action="github_account_linked",
            resource_kind="github_account_link",
            resource_id=str(user_id),
            # The login and the account id. NEVER the token: the record is built from `LinkRecord`,
            # which has no credential field, so this row cannot capture one by accident.
            reason=f"linked GitHub account {record.github_login} (id {record.github_user_id})",
            outcome="allowed",
            actor_kind="user",
            actor_user_id=user_id,
            tenant_id=tenant_id,
            before_state=None,
            after_state={"github_login": record.github_login, "github_user_id": record.github_user_id},
        ),
    )
    await session.commit()
    return _status(configured=True, record=record)


@router.delete("/github", response_model=DisconnectResponse, summary="Disconnect and revoke")
async def disconnect_github_link(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DisconnectResponse:
    """Delete the link and ask GitHub to invalidate the token.

    The local delete happens even when the revocation call fails, and the response says which — a
    disconnect that refused because GitHub was unreachable would block a user from doing the safe
    thing during exactly the incident in which they want to.
    """
    service = _service(request)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        try:
            login, revoked = await service.disconnect(
                session, user_id=principal.user_id, tenant_id=principal.tenant_id, client=client
            )
        except GitHubLinkNotFoundError as exc:
            raise problem(
                "github-link-absent",
                detail="There is no GitHub account linked to this user, so there is nothing to disconnect.",
            ) from exc

    await _writer(request).append(
        session,
        AuditDraft(
            action="github_account_unlinked",
            resource_kind="github_account_link",
            resource_id=str(principal.user_id),
            reason=(
                f"disconnected GitHub account {login}; "
                + ("GitHub confirmed the token is revoked" if revoked else "the token could not be revoked at GitHub")
            ),
            outcome="allowed",
            actor_kind="user",
            actor_user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            before_state={"github_login": login},
            after_state=None,
        ),
    )
    await session.commit()
    return DisconnectResponse(login=login, revoked_at_github=revoked)


@router.get(
    "/github/repositories",
    response_model=RepositoryPage,
    summary="The repositories this user's linked account can reach",
)
async def list_github_repositories(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    query: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> RepositoryPage:
    """Real API data, filtered and paged here.

    WHY THE SEARCH AND THE PAGING ARE DONE HERE rather than by GitHub. `GET /user/repos` paginates but
    does not search, and `GET /search/repositories` searches all of GitHub rather than what this user
    can reach — a query typed into the picker must not return a stranger's repository. So the walk over
    `/user/repos` is bounded, the filter is applied to what it returned, and `truncated` is reported
    when the bound was hit. Nothing is cached: a stale repository list is the failure mode where a user
    picks a repository that has been renamed, and the clone then fails naming neither.
    """
    service = _service(request)
    if not service.is_configured():
        raise _unconfigured()
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        try:
            repositories, truncated = await service.repositories(
                session, user_id=principal.user_id, tenant_id=principal.tenant_id, client=client
            )
        except GitHubLinkNotFoundError as exc:
            raise problem(
                "github-link-absent",
                detail=(
                    "No GitHub account is linked to this user, so there are no repositories to list. "
                    "Connect one from Settings → Integrations."
                ),
            ) from exc
        except GitHubAppNotConfiguredError as exc:
            raise _unconfigured(exc) from exc
        except (GitHubAppError, GitHubLinkError) as exc:
            raise problem("github-link-failed", detail=str(exc)) from exc
    await session.commit()

    matched = _filtered(repositories, query)
    start = (page - 1) * per_page
    window = matched[start : start + per_page]
    return RepositoryPage(
        # `asdict` rather than `vars`: `Repository` is a slotted dataclass, so it has no `__dict__`.
        items=[RepositoryItem(**asdict(item)) for item in window],
        total=len(matched),
        page=page,
        per_page=per_page,
        truncated=truncated,
    )


def _filtered(repositories: tuple[Repository, ...], query: str) -> list[Repository]:
    """Case-insensitive substring match over the full name and the primary language.

    Substring rather than prefix, because people remember the middle of a repository name; over the
    FULL name rather than the bare name, so `acme/` narrows to an organisation.
    """
    needle = query.strip().lower()
    if not needle:
        return list(repositories)
    return [item for item in repositories if needle in item.full_name.lower() or needle in item.language.lower()]
