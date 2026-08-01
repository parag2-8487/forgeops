# SPDX-License-Identifier: FSL-1.1-ALv2
"""The three agent-device routes (design.md §3.1, §4.4, §11.2, §14.6, Appendix A.1, Q-17, Q-19).

* `POST /api/v1/agents/pairing-codes` — issue a code. Admin or developer (§3.1).
* `POST /api/v1/agents/pair/exchange` — the **one** new public route (§4.4). No principal exists
  yet; this is the exchange that creates the credential.
* `DELETE /api/v1/agents/{device_id}` — revoke. Admin only (§11.2's resource table).

Why two routers in one module
-----------------------------
Deny-by-default is attached at the router (`dependencies=[Depends(require_principal)]`) so a route
added later is protected the moment it is declared, not when somebody remembers. That mechanism
cannot express "except this one path", and the exchange must be reachable without a principal. So
the public route lives on its own router with no dependency, which makes its exemption visible in
the code that declares it rather than in a path matcher — and `scripts/check-route-auth.py` cross-
checks both against `PUBLIC_ROUTES`.

What the response bodies do not contain
--------------------------------------
The exchange's 401 is one `pairing-code-invalid` for unknown, expired, burned, consumed and
"device row no longer pairable". The four internal branches reach the audit row's `failure_kind`
and stop there (Q-17). And no response, log line or problem `detail` on any path here carries the
code: the only place the code exists in the clear is the 201 body of the *issue* route, which is
what the operator reads.

Why the issue route is role-gated rather than Cerbos-gated
---------------------------------------------------------
§11.2's resource table gives `agent_device: pair` to admin and developer and `revoke` to admin
alone, and neither decision reads a resource attribute — which is exactly §11.2's own test for
what belongs in a role gate rather than in Cerbos ("Roles are coarse and static. Anything that
depends on the resource belongs in Cerbos"). §3.1's wording is "admin or developer **on the
project**", and the project-membership half is not implementable in Phase 1: D-40 defers `teams`
and `team_members` to Phase 2, so there is no membership edge to consult. The gap is real and is
recorded rather than papered over with a check that would always pass.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.errors import problem
from .ca import CertificateAuthorityUnavailableError
from .dependencies import require_principal, require_role
from .devices import (
    AgentMeta,
    CsrRejectedError,
    DeviceNotFoundError,
    DeviceService,
    PairingCodeInvalidError,
    PairingRateLimitedError,
)
from .models import UserRole
from .pairing_limits import PairingUnavailableError
from .principal import Principal

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["agents"],
    dependencies=[Depends(require_principal)],
)

#: The public half. Same prefix, no router-level dependency, exactly one route — and that route is
#: the single entry in `PUBLIC_ROUTES` that is not part of the auth flow.
public_router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

#: A generous ceiling on the submitted PEM. A P-256 CSR is ~450 bytes; 8 KiB leaves room for
#: extensions and refuses a body that could only be an attempt to make the parser work.
MAX_CSR_PEM_BYTES = 8192


class PairingCodeRequest(BaseModel):
    """Which project the code pairs a device to."""

    model_config = {"extra": "forbid"}

    project_id: uuid.UUID


class PairingCodeResponse(BaseModel):
    """§3.1's `201 {code, expires_at, device_id}`.

    The code appears here and nowhere else — not in a log line, not in an audit row, not in a
    database column. This response is the single moment it exists in the clear.
    """

    code: str
    device_id: uuid.UUID
    expires_at: str


class ExchangeRequest(BaseModel):
    """§3.1's `{code, csr, agent_version, platform, fingerprint}`.

    `extra="forbid"` because this is the one route an unauthenticated caller can reach: a body
    with unknown members is either a client bug or a probe, and accepting it silently makes both
    invisible.
    """

    model_config = {"extra": "forbid"}

    code: str = Field(min_length=1, max_length=64)
    csr: str = Field(min_length=1, max_length=MAX_CSR_PEM_BYTES)
    agent_version: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    #: SHA-256 of the CSR's SubjectPublicKeyInfo DER, lowercase hex. Checked against the CSR, so
    #: a mismatch is a rejection rather than a value the server quietly prefers.
    fingerprint: str = Field(min_length=64, max_length=64, pattern="^[0-9a-fA-F]{64}$")


class ExchangeResponse(BaseModel):
    """What the exchange issues (§3.1's `201` body, as far as Phase 1 has built it).

    §3.1's response also carries `policy_bundle` and `policy_bundle_digest`. Both come from
    `PolicyBundleService.publish`, which leaf 9.3 builds; they are absent rather than empty,
    because D-30 makes a missing bundle a **deny** on the agent side, so a zero-byte bundle would
    be a field that means "refuse everything" while looking like a bundle.

    The token and the key are hex-encoded. They are 32 random bytes each and JSON has no byte
    type; hex rather than base64url because the agent stores them as bytes and one decoding
    mistake there is a credential that silently never matches. The certificate and CA bundle are
    PEM strings, which are already text and already have exactly one spelling.
    """

    device_id: uuid.UUID
    project_id: uuid.UUID
    device_token: str
    envelope_key: str
    csr_spki_sha256: str
    client_cert: str
    ca_bundle: str
    cert_serial: str
    cert_fingerprint: str
    cert_not_after: str
    renew_after: str


class RevokeRequest(BaseModel):
    """Why the device is being revoked. Required, per NFR-14's "why"."""

    model_config = {"extra": "forbid"}

    reason: str = Field(min_length=1, max_length=500)


def _service(request: Request) -> DeviceService:
    """The composed `DeviceService` from `app.state`.

    Read from state rather than constructed here: the composition root wires the audit recorder,
    the Redis client and the two rate-limit buckets, and a route that built its own service would
    build the custody-only form and fail at the first exchange.
    """
    service: DeviceService = request.app.state.device_service
    return service


@router.post(
    "/pairing-codes",
    response_model=PairingCodeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a single-use pairing code for a project",
    dependencies=[Depends(require_role(UserRole.ADMIN, UserRole.DEVELOPER))],
)
async def issue_pairing_code(
    body: PairingCodeRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PairingCodeResponse:
    """Issue a code, revoking any code already live for the project (Appendix A.1)."""
    issued = await _service(request).issue_pairing_code(session, project_id=body.project_id, actor=principal)
    return PairingCodeResponse(
        code=issued.code,
        device_id=issued.device_id,
        expires_at=issued.expires_at.isoformat(),
    )


@public_router.post(
    "/pair/exchange",
    response_model=ExchangeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Exchange a pairing code for device credentials (public)",
)
async def exchange_pairing_code(
    body: ExchangeRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExchangeResponse:
    """The public exchange. Rate limited per IP and globally before anything else runs."""
    client_ip = request.client.host if request.client else ""
    try:
        credentials = await _service(request).exchange(
            session,
            code=body.code,
            csr_pem=body.csr.encode("utf-8"),
            meta=AgentMeta(
                agent_version=body.agent_version,
                platform=body.platform,
                fingerprint=body.fingerprint,
            ),
            client_ip=client_ip,
        )
    except PairingRateLimitedError as exc:
        raise problem(
            "pairing-rate-limited",
            detail=f"Too many pairing attempts. Retry after {exc.retry_after_seconds} seconds.",
        ) from exc
    except PairingUnavailableError as exc:
        raise problem(
            "pairing-unavailable",
            detail="The pairing service cannot evaluate its rate limits right now.",
        ) from exc
    except CertificateAuthorityUnavailableError as exc:
        # Mapped onto the same 503 as a Redis outage, and for the same reason: the exchange cannot
        # complete, the client should retry with backoff, and the `detail` must not name a
        # configuration variable to an unauthenticated caller. The operator's diagnostic is the log
        # line the exception carries, not this body.
        raise problem(
            "pairing-unavailable",
            detail="The pairing service cannot issue a device certificate right now.",
        ) from exc
    except CsrRejectedError as exc:
        # A distinct, 400-shaped answer, and it is safe to distinguish: the CSR is public and the
        # check runs BEFORE the code is consumed, so this response tells a caller nothing about
        # whether the code it sent exists.
        raise problem("csr-invalid", detail=str(exc)) from exc
    except PairingCodeInvalidError as exc:
        raise problem(
            "pairing-code-invalid",
            detail="The pairing code is not valid. Issue a new one and try again.",
        ) from exc
    return ExchangeResponse(
        device_id=credentials.device_id,
        project_id=credentials.project_id,
        device_token=credentials.device_token.get_secret_value().hex(),
        envelope_key=credentials.envelope_key.get_secret_value().hex(),
        csr_spki_sha256=credentials.csr_spki_sha256,
        client_cert=credentials.client_cert_pem.decode("utf-8"),
        ca_bundle=credentials.ca_bundle_pem.decode("utf-8"),
        cert_serial=credentials.cert_serial,
        cert_fingerprint=credentials.cert_fingerprint,
        cert_not_after=credentials.cert_not_after.isoformat(),
        renew_after=credentials.renew_after.isoformat(),
    )


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a device",
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)
async def revoke_device(
    device_id: uuid.UUID,
    body: RevokeRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Revoke a device. Idempotent: a second call succeeds and writes no second audit row."""
    try:
        await _service(request).revoke(session, device_id=device_id, actor=principal, reason=body.reason)
    except DeviceNotFoundError as exc:
        # The same non-disclosing body a permission failure would produce would be wrong here —
        # the caller is an admin who may read every device — so a plain 404 is correct. §4.2's
        # enumeration rule is about the 403 body, not about admin-scoped 404s.
        raise problem("device-not-found", detail="No such device.") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
