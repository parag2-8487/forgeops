# SPDX-License-Identifier: FSL-1.1-ALv2
"""Device read surface (design.md §3.1, §3.7, §11.2; criterion 10 step 4).

`agent_routes.py` published a POST to mint a pairing code, a public POST to exchange one, and a
DELETE to revoke — and **no GET**. So pairing was write-only: a device could be created and destroyed
but never observed, which is why §12.6 step 4 ("assert the device is active and heartbeating") had no
endpoint to assert against, and why the `/pairing` screen had nothing to read.

Kept in its own module rather than appended to `agent_routes.py` for one reason: that file's two
routers exist to make the single public exemption visible where it is declared, and adding read
routes into it would bury that distinction in a longer file. These are all authenticated.

**What is deliberately not exposed.** `pairing_token_hmac`, `device_token_hmac` and
`envelope_key_enc` are columns on `agent_devices` and none of them appears in the response model. The
first two are HMACs of bearer credentials and the third is a wrapped key; a read surface that
returned them would turn "list my devices" into credential exfiltration. `cert_fingerprint` IS
returned, because a fingerprint is a public identifier of a certificate and is what an operator
compares against the agent's own log.

**Heartbeat is reported as an observation, never as a claim.** `AgentPairing.tsx` displayed the
status "Connected & Attested" with no props and no fetch, asserting a verified connection nothing had
checked. So this returns `last_seen` and a `heartbeat_fresh` that is **`None` when the device has
never been seen** — tri-state on purpose, because `False` would say "we looked and it is stale" where
the truth is "it has never reported". A boolean cannot express the difference and the difference is
the whole defect.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.errors import forbidden_problem
from .dependencies import require_principal
from .device_models import DeviceStatus
from .principal import Principal

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["agents"],
    dependencies=[Depends(require_principal)],
)

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

#: Every column safe to return. Written out rather than `SELECT *` precisely because three columns
#: on this table are credential material: an explicit list cannot silently start including a new
#: secret column added by a later migration.
_COLUMNS = (
    "id, project_id, status, agent_version, platform, cert_serial, cert_fingerprint, "
    "cert_not_after, last_seq, last_seen, pairing_expires_at, revoked_at, created_at"
)


class DeviceRead(BaseModel):
    """One agent device, as observed."""

    id: uuid.UUID
    project_id: uuid.UUID
    status: DeviceStatus = Field(description="§3.7's state machine: pending, active, policy_stale, revoked, abandoned.")
    agent_version: str
    platform: str
    cert_serial: str | None = None
    cert_fingerprint: str | None = None
    cert_not_after: datetime | None = None
    last_seq: int
    last_seen: datetime | None = None
    pairing_expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime

    #: Seconds since the last observed heartbeat, or `None` if the device has never reported.
    seconds_since_last_seen: int | None = None
    #: `True` heard from within the timeout, `False` heard from but stale, `None` never heard from.
    #: See the module docstring: the third case is why this is not a boolean.
    heartbeat_fresh: bool | None = None
    #: The timeout the two fields above were evaluated against, so a client renders the same
    #: judgement the server made rather than inventing its own threshold.
    heartbeat_timeout_seconds: int


class DevicePage(BaseModel):
    devices: list[DeviceRead]
    next_cursor: str | None = None


def _timeout(request: Request) -> int:
    settings = getattr(request.app.state, "settings", None)
    return int(getattr(settings, "heartbeat_timeout_seconds", 90))


def _to_read(row: dict[str, Any], *, timeout_seconds: int) -> DeviceRead:
    """Project a row, deriving the heartbeat observation from `last_seen`."""
    last_seen = row.get("last_seen")
    if last_seen is None:
        seconds: int | None = None
        fresh: bool | None = None
    else:
        # `last_seen` is `timestamptz`, so it arrives aware; comparing against an aware now keeps
        # this correct regardless of the server's local zone.
        reference = last_seen if last_seen.tzinfo is not None else last_seen.replace(tzinfo=UTC)
        seconds = max(0, int((datetime.now(UTC) - reference).total_seconds()))
        fresh = seconds <= timeout_seconds
    return DeviceRead(
        **row,
        seconds_since_last_seen=seconds,
        heartbeat_fresh=fresh,
        heartbeat_timeout_seconds=timeout_seconds,
    )


def _tenant_clause(tenant_id: uuid.UUID | None) -> str:
    return "tenant_id IS NULL" if tenant_id is None else "tenant_id = :tenant_id"


def _tenant_params(tenant_id: uuid.UUID | None) -> dict[str, Any]:
    return {} if tenant_id is None else {"tenant_id": tenant_id}


@router.get("/devices", response_model=DevicePage, summary="List paired agent devices")
async def list_devices(
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    project_id: uuid.UUID | None = None,
    status: DeviceStatus | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> DevicePage:
    """The devices the caller's tenant can see, newest first.

    `status` is typed as `DeviceStatus`, so an unknown value is FastAPI's registered
    `validation-failed` 422 rather than a query that matches nothing and looks like an empty
    inventory.
    """
    clauses = [_tenant_clause(principal.tenant_id)]
    params: dict[str, Any] = {"limit": limit, **_tenant_params(principal.tenant_id)}
    if project_id is not None:
        clauses.append("project_id = :project_id")
        params["project_id"] = project_id
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status.value

    result = await session.execute(
        text(
            f"SELECT {_COLUMNS} FROM agent_devices WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT :limit"
        ),
        params,
    )
    timeout_seconds = _timeout(request)
    return DevicePage(devices=[_to_read(dict(row), timeout_seconds=timeout_seconds) for row in result.mappings()])


@router.get("/devices/{device_id}", response_model=DeviceRead, summary="Read one agent device")
async def get_device(
    device_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceRead:
    """One device, or the non-disclosing 403.

    Not `device-not-found`, even though that type is registered and the revoke route uses it. The
    difference is what the caller has already proved: revoke is scoped to a device the caller is
    acting on, whereas this is a read by id, and answering 404 here would let a caller enumerate
    device ids across tenants. §4.2 requires the forbidden body to be identical whether or not the
    row exists.
    """
    result = await session.execute(
        text(f"SELECT {_COLUMNS} FROM agent_devices WHERE id = :id AND {_tenant_clause(principal.tenant_id)}"),
        {"id": device_id, **_tenant_params(principal.tenant_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise forbidden_problem()
    return _to_read(dict(row), timeout_seconds=_timeout(request))
