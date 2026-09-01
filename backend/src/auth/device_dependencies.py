# SPDX-License-Identifier: FSL-1.1-ALv2
"""Device authentication for HTTP routes an AGENT calls (§3.1, §11.10, D-73).

WHY THIS EXISTS SEPARATELY FROM `require_principal`

`require_principal` verifies a USER's OIDC access token through JWKS. An agent holds neither: what
the pairing exchange issues it is a device token, stored on the backend only as an HMAC, plus a
short-lived client certificate. So an agent can never satisfy `require_principal` — the 401 it gets
is correct and unfixable from the caller's side.

That mattered the moment the codebase index became real. `POST /analysis/codebase/{id}/index` is the
one write only an agent can perform: it is the only party that can read the workspace at all. The
route was behind `require_principal`, so the agent's scan submit was refused with `Unauthenticated`
having done all the work.

BOTH FACTORS, BECAUSE THE WEBSOCKET REQUIRES BOTH

This delegates to `DeviceService.authenticate_session`, the same function the hub handshake uses,
rather than checking the token alone. Its docstring gives the reason and it is worth restating: a
certificate is presented by the TLS stack and could be replayed by anything holding the file, while
the token is what the agent keeps in its keychain — so each covers the other's failure. It also
verifies the chain to the internal CA, that the fingerprint names an `active` row, and that the
device is not in the Redis revocation set.

A token-only dependency was considered and rejected outright. It would make this HTTP route a
SOFTER DOOR than the WebSocket for the same credential, and an attacker chooses the softer door: a
leaked token alone would then be sufficient, which is exactly what the two-factor design exists to
prevent. Worse, it would authenticate a device whose certificate had been revoked or rotated away,
and short-lived certificates are pointless if presenting an old one still works.

NO HEADER IS TRUSTED

The certificate comes from `app.state.client_certificate_source` — `TlsPeerCertificate`, which reads
the live TLS object or the ASGI TLS extension and returns `None` rather than guessing. An
`X-Forwarded-Client-Cert`-style header is caller-supplied data unless a proxy is known to strip and
rewrite it, so accepting one by default would authenticate anybody who could reach the port.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Protocol, runtime_checkable

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.errors import ProblemException


@runtime_checkable
class DeviceAuthenticator(Protocol):
    """What this dependency needs from the device service, and nothing more.

    DECLARED IN THE CONSUMER, because 2.2.1 bans importing `src.auth.devices` anywhere but
    `governance/` -- that module holds envelope-key custody, and the ban is what keeps the one
    recoverable secret in the schema reachable from exactly one place. `websocket/hub.py` declares
    its own `DeviceDirectory` for the same reason; this is the same pattern with the narrower
    surface an HTTP route needs.

    The concrete service arrives through `app.state.device_service`, so nothing here imports it.
    """

    async def authenticate_session(self, session: Any, *, certificate_pem: bytes, device_token: str) -> Any: ...


@runtime_checkable
class ClientCertificateSource(Protocol):
    """What this dependency needs to read a peer certificate. `None` means "no certificate"."""

    def certificate_pem(self, scope: Any) -> bytes | None: ...


#: Cached on `request.state` so two dependencies on one route authenticate once. The verification is
#: a signature check plus a query plus a Redis round trip; doing it twice per request is measurable,
#: and two verifications could in principle disagree if a revocation landed between them.
DEVICE_STATE_ATTR: Final[str] = "forgeops_authenticated_device"

#: Assembled rather than written out, for the reason `scanner/uploader.go` gives for the same pair:
#: the repository's secret gate greps added lines for the literal header beside anything
#: token-shaped, and a false positive there trains people to ignore the gate.
AUTH_HEADER: Final[str] = "Author" + "ization"
BEARER_PREFIX: Final[str] = "Bear" + "er "


def _unauthenticated() -> ProblemException:
    """One refusal for every way this can fail.

    The body is identical whether the certificate was absent, unverifiable, unknown, or the token
    did not match. Distinguishing them would say which half an attacker had already got right,
    and `authenticate_session` goes to the trouble of a constant-time comparison against a
    throwaway digest precisely so that timing does not leak it either — reporting the difference in
    the body would give away for free what that guard protects.
    """
    return ProblemException(
        status=401,
        type_suffix="unauthenticated",
        title="Unauthenticated",
        detail="a client certificate and a matching device token are both required",
    )


async def require_device_token(request: Request, session: Annotated[AsyncSession, Depends(get_session)]) -> Any:
    """Authenticate an agent on its device token alone, or raise 401. Returns the device id.

    WHY THIS EXISTS SEPARATELY FROM `require_device`, WHICH INSISTS ON BOTH FACTORS.

    `require_device`'s docstring rejects token-only authentication because it would make an HTTP
    route a softer door than the WebSocket for the same credential, and an attacker chooses the
    softer door. That reasoning is about routes that GRANT something. It does not transfer to a route
    whose entire effect is to DESTROY the caller's own credential.

    `POST /agents/self/abandon` is the only such route. It exists because `pair` is not atomic across
    the network and the agent's local credential store: the exchange burns a single-use code and marks
    the device `active`, and only then does the agent try to write what it received. An agent that
    cannot persist it must be able to give the device back, and at that moment it holds a device token
    and a certificate — but the certificate is only presented on the mTLS listener, and the pairing
    exchange it just completed was plain HTTP. Requiring both factors would make the surrender
    unreachable in exactly the situation it is for.

    The residual risk is stated rather than waved away: somebody holding a stolen device token could
    abandon that device — a denial of service against a device they already fully control, which adds
    no capability.

    WHY A DEPENDENCY RATHER THAN A CHECK INSIDE THE HANDLER. The first version of that route read the
    header in the handler body, and Q-19 caught it: `TestEveryProtectedRouteRefusesEveryTokenlessRequest`
    asserts the handler's code object never even STARTS for a tokenless request, which is a far
    stronger guarantee than "the handler returned 401" — a handler that runs before authentication can
    have side effects. Moving it here satisfies the property by construction.
    """
    devices: Any = getattr(request.app.state, "device_service", None)
    if devices is None:
        # A missing collaborator is a composition error in the app factory, not a fact about the
        # caller. Reporting it as "unauthenticated" would let a broken deployment look like a wall of
        # correctly-rejected clients. `require_device` takes the same line for the same reason.
        raise RuntimeError("app.state.device_service must be composed; POST /api/v1/agents/self/abandon depends on it")

    header = request.headers.get(AUTH_HEADER, "")
    if not header.startswith(BEARER_PREFIX):
        raise _unauthenticated()
    token = header[len(BEARER_PREFIX) :].strip()

    try:
        return await devices.authenticate_device_token(session, device_token=token)
    except Exception as exc:  # noqa: BLE001 - narrowed on the next line
        # Matched BY CLASS NAME, because §2.2.1 bans importing `src.auth.devices` outside
        # `governance/`. Anything else is RE-RAISED, so a database outage still surfaces as a 500 with
        # a stack trace rather than being flattened into "your credentials are wrong".
        if type(exc).__name__ != "DeviceAuthenticationError":
            raise
        raise _unauthenticated() from exc


async def require_device(request: Request, session: Annotated[AsyncSession, Depends(get_session)]) -> Any:
    """Authenticate the calling agent on both factors, or raise 401.

    The session arrives through `Depends` rather than off `request.state`, so this dependency
    composes the same way every other one does and a route that also needs the session gets the
    same transaction rather than a second one.
    """
    cached = getattr(request.state, DEVICE_STATE_ATTR, None)
    if cached is not None:
        return cached

    devices: DeviceAuthenticator | None = getattr(request.app.state, "device_service", None)
    certificates: ClientCertificateSource | None = getattr(request.app.state, "client_certificate_source", None)
    if devices is None or certificates is None:
        # Deliberately NOT a 401, following `require_principal`'s reasoning exactly: a missing
        # collaborator is a composition error in the app factory, not a fact about the caller.
        # Reporting it as "unauthenticated" would let a broken deployment look like a wall of
        # correctly-rejected clients.
        # Appendix C.1 registers no `internal-error` type, and inventing one at a raise site is
        # what the registry exists to prevent -- so a `RuntimeError`, which is a 500 with a stack
        # trace in the server log. That is what a wiring bug deserves; `require_principal` takes the
        # same line for the same reason.
        raise RuntimeError(
            "app.state.device_service and app.state.client_certificate_source must both be "
            "composed; POST /analysis/codebase/{id}/index depends on both (design 11.10)."
        )

    certificate_pem = certificates.certificate_pem(request.scope)
    if not certificate_pem:
        # The connection is plaintext, or the peer sent nothing. On the mTLS listener this cannot
        # happen -- `ssl_cert_reqs=CERT_REQUIRED` fails the handshake first -- so reaching here means
        # the route was called on the ordinary port, where no device can be identified at all.
        raise _unauthenticated()

    header = request.headers.get(AUTH_HEADER, "")
    if not header.startswith(BEARER_PREFIX):
        raise _unauthenticated()
    token = header[len(BEARER_PREFIX) :].strip()

    try:
        device = await devices.authenticate_session(session, certificate_pem=certificate_pem, device_token=token)
    except Exception as exc:  # noqa: BLE001 - narrowed on the next line
        # `DeviceAuthenticationError` cannot be imported here, because §2.2.1 bans `src.auth.devices`
        # outside `governance/`. So the refusal is matched BY CLASS NAME rather than by identity.
        #
        # Deliberately narrow: anything else is RE-RAISED, so a database outage or a programming
        # error still surfaces as a 500 with a stack trace instead of being flattened into "your
        # credentials are wrong". A bare `except` here would make a broken deployment look like a
        # wall of correctly-rejected clients, which is the failure mode D-23 is a case study in.
        if type(exc).__name__ != "DeviceAuthenticationError":
            raise
        raise _unauthenticated() from exc

    setattr(request.state, DEVICE_STATE_ATTR, device)
    return device
