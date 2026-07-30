# SPDX-License-Identifier: FSL-1.1-ALv2
"""`/api/v1/auth/*` — login, callback, refresh, logout (design.md §3.5, §11.2, §13.1).

All four routes are in `PUBLIC_ROUTES` and none carries `require_principal`: the flow
that *creates* a principal cannot require one, and logout must succeed after the access
token has already expired. That exemption is committed in `public_routes.py` with its
reason, and `scripts/check-route-auth.py` asserts the set against the real router — so
these four being unauthenticated is a deliberate, reviewed fact rather than an omission.

The cookie carries the refresh token; the body carries the access token
--------------------------------------------------------------------------
§3.5 requires an httpOnly `SameSite=Lax` cookie and the access token in the body. The
split is the point: the access token is short-lived and the frontend must send it as a
bearer header on every call, so script has to be able to read it; the refresh token is
long-lived, is never needed by script, and is therefore put where script cannot reach
it. Putting the access token in the cookie too would make every authenticated request
ambiently authorised and reintroduce CSRF on the whole API. `sessions.py` documents why
it is the refresh token in the cookie rather than an opaque session id.

`next` is validated, not trusted
--------------------------------
`/login?next=` is an open-redirect surface if it is echoed back. Only a same-origin
absolute path is accepted; anything else falls back to `/`. The value is returned in the
callback's JSON body rather than issued as a `Location`, so even a bypass could not
produce a redirect to another origin from this endpoint.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, RedirectResponse

from ..core.db import get_session
from ..core.errors import problem
from .oidc import (
    PKCE_STATE_KEY_PREFIX,
    PKCE_STATE_TTL_SECONDS,
    OidcClient,
    PendingLogin,
    TokenResponse,
    access_token_expiry,
    role_from_groups,
)
from .sessions import SessionService

router = APIRouter(prefix="/auth", tags=["auth"])

#: Environments in which the session cookie may travel over plain HTTP. Anything else
#: gets `Secure`, so a production deployment cannot accidentally ship a cookie that a
#: downgraded connection can read.
_INSECURE_COOKIE_ENVS: Final[frozenset[str]] = frozenset({"local", "development", "dev", "test"})

#: The default landing path when `next` is absent or rejected.
_DEFAULT_NEXT: Final[str] = "/"


def _safe_next(raw: str | None) -> str:
    """A same-origin absolute path, or `/`.

    Three rejections, each for a concrete reason: anything not starting with `/` could
    be an absolute URL to another origin; anything starting with `//` is
    protocol-relative and *is* another origin; anything containing a backslash is
    rejected because some user agents normalise `/\\evil.example` into a
    protocol-relative URL, which would slip past a check that only looked for `//`.

    A colon elsewhere in the path is fine — `/a:b` is a legal same-origin path and no
    user agent reads it as a scheme once it begins with `/`.
    """
    if not raw or not raw.startswith("/"):
        return _DEFAULT_NEXT
    if raw.startswith("//") or "\\" in raw:
        return _DEFAULT_NEXT
    return raw


def _client(request: Request) -> OidcClient:
    client = getattr(request.app.state, "oidc_client", None)
    if client is None:
        # A composition error, not a fact about the caller — the same reasoning as
        # `dependencies.require_principal`. A 500 with a stack trace is what a wiring
        # bug deserves; the §0.4.1 wiring test is what stops it reaching a deployment.
        raise RuntimeError(
            "app.state.oidc_client is not composed; the auth flow depends on it "
            "(design §11.2). create_app() must build it in the lifespan."
        )
    return client


def _sessions(request: Request) -> SessionService:
    service = getattr(request.app.state, "session_service", None)
    if service is None:
        raise RuntimeError(
            "app.state.session_service is not composed; the auth flow depends on it "
            "(design §11.2). create_app() must build it in the lifespan."
        )
    return service


def _set_session_cookie(request: Request, response: Response, refresh_token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=settings.session_cookie_name,
        value=refresh_token,
        max_age=settings.refresh_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=str(settings.app_env).lower() not in _INSECURE_COOKIE_ENVS,
        path="/",
    )


def _clear_session_cookie(request: Request, response: Response) -> None:
    settings = request.app.state.settings
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        samesite="lax",
        secure=str(settings.app_env).lower() not in _INSECURE_COOKIE_ENVS,
        path="/",
    )


async def _store_pending(request: Request, state: str, pending: PendingLogin) -> None:
    await request.app.state.redis.set(
        f"{PKCE_STATE_KEY_PREFIX}{state}",
        pending.to_json(),
        ex=PKCE_STATE_TTL_SECONDS,
    )


async def _take_pending(request: Request, state: str) -> PendingLogin | None:
    """Consume the pending-login record. Single-use, atomically.

    `GETDEL` rather than `GET` then `DELETE`: the two-step version lets a replayed
    `state` be accepted twice if both arrive before the delete lands, which is exactly
    the race the single-use rule exists to close.
    """
    raw = await request.app.state.redis.getdel(f"{PKCE_STATE_KEY_PREFIX}{state}")
    if raw is None:
        return None
    try:
        return PendingLogin.from_json(raw)
    except (ValueError, KeyError, TypeError):
        return None


@router.api_route("/login", methods=["GET", "POST"])
async def login(request: Request) -> RedirectResponse:
    """Begin the authorization-code + PKCE flow (§3.5 steps 2–3).

    302 rather than a JSON body carrying the URL: the browser has to end up at the IdP,
    and a redirect keeps that a property of the endpoint instead of something every
    caller has to reimplement.
    """
    client = _client(request)
    next_path = _safe_next(request.query_params.get("next"))
    authorization, pending = await client.authorization_request(next_path=next_path)
    await _store_pending(request, authorization.state, pending)
    # 302 and not 307: the browser must issue a GET to the IdP regardless of how it
    # reached /login, and 307 would preserve a POST.
    return RedirectResponse(url=authorization.url, status_code=302)


@router.api_route("/callback", methods=["GET", "POST"])
async def callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Complete the exchange, upsert the user, open a session (§3.5 steps 5–11)."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise problem("unauthenticated")

    pending = await _take_pending(request, state)
    if pending is None:
        # Covers an unknown state, an expired one, and a REPLAYED one — the record is
        # consumed by the first use, so a second callback with the same state lands
        # here. One response for all three: which of them it was is not information the
        # caller needs, and the one who benefits from knowing is the one guessing.
        raise problem("unauthenticated")

    client = _client(request)
    tokens = await client.exchange_code(code=code, verifier=pending.verifier)
    body = await _open_session(request, session, tokens, nonce=pending.nonce)
    body["next"] = pending.next_path

    response = JSONResponse(body)
    if tokens.refresh_token:
        _set_session_cookie(request, response, tokens.refresh_token)
    return response


@router.post("/refresh")
async def refresh(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Rotate the session and return a fresh access token."""
    settings = request.app.state.settings
    presented = request.cookies.get(settings.session_cookie_name)
    if not presented:
        raise problem("unauthenticated")

    service = _sessions(request)
    current = await service.find_active(session, refresh_token=presented)
    if current is None:
        # Not found, expired, or already rotated. A replayed token that was rotated a
        # moment ago lands here, which is the detection the revoke-then-insert scheme
        # buys (see sessions.py).
        raise problem("unauthenticated")

    client = _client(request)
    tokens = await client.refresh(refresh_token=presented)
    # An IdP that does not rotate returns no new refresh token; carrying the presented
    # one forward keeps the cookie valid rather than silently ending the session.
    new_refresh = tokens.refresh_token or presented
    rotated = await service.rotate(session, current=current, new_refresh_token=new_refresh)

    claims = await _verified_id_claims(request, tokens, nonce=None)
    body = _token_body(tokens, claims, session_id=rotated.id)
    response = JSONResponse(body)
    _set_session_cookie(request, response, new_refresh)
    return response


@router.api_route("/logout", methods=["GET", "POST"])
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """End the session and clear the cookie. Always 200 (§4.4).

    Deliberately succeeds when there is no live session: the common case is a user
    clicking log out after their token expired, and a 401 there would leave the cookie
    in place — the browser would keep presenting a credential the server has already
    forgotten.
    """
    settings = request.app.state.settings
    presented = request.cookies.get(settings.session_cookie_name)
    if presented:
        await _sessions(request).revoke_by_token(session, refresh_token=presented)

    response = JSONResponse({"status": "logged_out"})
    _clear_session_cookie(request, response)
    return response


async def _verified_id_claims(request: Request, tokens: TokenResponse, *, nonce: str | None) -> Any:
    verifier = getattr(request.app.state, "id_token_verifier", None)
    if verifier is None:
        raise RuntimeError(
            "app.state.id_token_verifier is not composed; the auth flow depends on it "
            "(design §11.2). create_app() must build it in the lifespan."
        )
    return await verifier.verify_id_token(tokens.id_token, nonce=nonce)


async def _open_session(
    request: Request,
    session: AsyncSession,
    tokens: TokenResponse,
    *,
    nonce: str,
) -> dict[str, Any]:
    claims = await _verified_id_claims(request, tokens, nonce=nonce)
    raw = claims.raw

    role = role_from_groups(raw.get("groups"))
    email = str(raw.get("email") or "")
    name = str(raw.get("name") or raw.get("preferred_username") or email or claims.sub)

    service = _sessions(request)
    user = await service.upsert_user(
        session,
        idp_subject=claims.sub,
        email=email or f"{claims.sub}@users.noreply.invalid",
        name=name,
        role=role,
    )
    # No refresh token means no session to persist. An IdP that was not asked for — or
    # refuses — `offline_access` returns none, and inserting a row keyed on
    # `HMAC(pepper, "")` would create a lookup key every future tokenless login shares,
    # so `find_active` would match an arbitrary one of them. The access token still
    # works until it expires; the caller simply cannot refresh, which is the truth.
    session_id: Any = None
    if tokens.refresh_token:
        opened = await service.create_session(
            session,
            user_id=user.id,
            refresh_token=tokens.refresh_token,
            idp_session_id=str(raw.get("sid") or "") or None,
        )
        session_id = opened.id
    body = _token_body(tokens, claims, session_id=session_id)
    body.update({"user_id": str(user.id), "email": user.email, "role": user.role.value})
    return body


def _token_body(tokens: TokenResponse, claims: Any, *, session_id: Any) -> dict[str, Any]:
    """The response body. Carries the access token and nothing derived from a secret."""
    return {
        "access_token": tokens.access_token,
        "token_type": "Bearer",
        "expires_in": access_token_expiry(tokens.access_token, tokens.expires_in),
        "session_id": None if session_id is None else str(session_id),
        "subject": claims.sub,
    }
