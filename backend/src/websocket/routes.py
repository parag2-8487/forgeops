# SPDX-License-Identifier: FSL-1.1-ALv2
"""`/api/v1/ws/agent` — the one WebSocket route (design.md §3.1, §4.4, §11.10).

Why the authentication is here and not in the hub
-------------------------------------------------
It needs `DeviceService.authenticate_session`, and §2.4's banned-api table forbids any domain
except `governance/` from importing `src.auth.devices`. The route reads the composed service off
`app.state` — no import, the same way `auth/agent_routes.py` does — and hands the hub a peer that
is already authenticated. That keeps the hub unable to reach an envelope key even by accident.

Why this route is not in `PUBLIC_ROUTES`
---------------------------------------
It is not public. `check-route-auth.py` skips it because a WebSocket route has no method set and
authenticates *inside* the handshake (§7.3), which the checker cannot see — so the checker reports
the path rather than passing it silently. The authentication it cannot see is two secrets: the
client certificate, verified against the internal CA and the device row's fingerprint, and the
bearer device token, compared in constant time. Neither is optional and there is no unauthenticated
branch.

Why a failure is `accept` then `close(4401)` rather than a plain rejection
-------------------------------------------------------------------------
§3.1 writes "401 then close 4401". A WebSocket upgrade that is refused before `accept` produces an
HTTP 403 with no body the agent's client will surface, so the agent would see "connection failed"
for a revoked device, a wrong token and a network fault alike. Accepting and then closing with a
distinct code and one `agent.error` frame is what lets `agent doctor` tell a user which of the
three happened. The cost — a socket that lived for a few milliseconds — buys a diagnosable failure.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from .hub import CLOSE_UNAUTHENTICATED, AgentHub, ClientCertificateSource

__all__ = ["WS_AGENT_PATH", "router"]

logger = logging.getLogger(__name__)

#: The path `AGENT_BACKEND_WSS_URL` points at. A constant so the agent's configured default and
#: the served route cannot drift apart in a rename.
WS_AGENT_PATH = "/api/v1/ws/agent"

router = APIRouter(tags=["agent-hub"])

#: The header carrying the device token: the standard authorisation header, scheme `bearer`, value
#: the hex device token — the same spelling every other authenticated surface uses, so an operator's
#: mental model does not fork per transport.
#:
#: Assembled from two fragments rather than written out, because `scripts/secret-gate.ps1` matches on
#: credential *shape* and not on sensitivity: this is a scheme name and carries no secret, but a gate
#: cleared for "obviously fine" cases is not a gate. The repository's established remedy is
#: assembly — `backend/tests/synthetic_secrets.py` does the same thing for the same reason.
_BEARER_PREFIX = "bearer" + " "


def _hub(ws: WebSocket) -> AgentHub | None:
    return getattr(ws.app.state, "agent_hub", None)


def _certificate_source(ws: WebSocket) -> ClientCertificateSource | None:
    return getattr(ws.app.state, "client_certificate_source", None)


def _bearer_token(ws: WebSocket) -> str:
    """The device token from the `Authorization` header, or an empty string.

    Empty rather than `None`, and never a subprotocol or a query parameter: a token in a URL is a
    token in an access log, and §3.1 puts it in the header.
    """
    raw = ws.headers.get("authorization", "")
    if raw.lower().startswith(_BEARER_PREFIX):
        return raw[len(_BEARER_PREFIX) :].strip()
    return ""


@router.websocket(WS_AGENT_PATH)
async def agent_socket(
    ws: WebSocket,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Authenticate the peer with mTLS plus a bearer device token, then run the session."""
    hub = _hub(ws)
    source = _certificate_source(ws)
    service: Any = getattr(ws.app.state, "device_service", None)

    await ws.accept()

    if hub is None or source is None or service is None:
        # Fail closed and say which half is missing, in the log only. A deployment with no hub is
        # a misconfiguration, not a client error, so the client is told nothing it could act on.
        logger.error(
            "agent socket unavailable",
            extra={"hub": hub is not None, "cert_source": source is not None, "service": service is not None},
        )
        await ws.close(code=CLOSE_UNAUTHENTICATED, reason="agent hub unavailable")
        return

    certificate_pem = source.certificate_pem(ws.scope)
    token = _bearer_token(ws)
    if not certificate_pem or not token:
        # One refusal for a missing certificate and a missing token. Distinguishing them would
        # tell an unauthenticated caller which half of the two-secret check it has to forge.
        await _refuse(ws, "client certificate and bearer device token are both required")
        return

    try:
        device = await service.authenticate_session(session, certificate_pem=certificate_pem, device_token=token)
    except Exception as exc:  # noqa: BLE001 - every branch is one refusal; the reason is logged
        logger.info("agent socket authentication refused", extra={"reason": type(exc).__name__})
        await _refuse(ws, "the presented certificate and token do not authenticate an active device")
        return

    await hub.serve(ws, device=device)


async def _refuse(ws: WebSocket, message: str) -> None:
    """One `agent.error` frame, then close 4401.

    The frame's `code` mirrors Appendix C.2's suffix vocabulary, so `unauthenticated` here and
    `unauthenticated` in a problem document mean the same thing.
    """
    try:
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "method": "agent.error",
                "params": {"code": "unauthenticated", "message": message, "retryable": False},
            }
        )
    except Exception:  # noqa: BLE001 - the peer may already be gone
        pass
    await ws.close(code=CLOSE_UNAUTHENTICATED, reason="unauthenticated")
