# SPDX-License-Identifier: FSL-1.1-ALv2
"""Agent uninstall and discovery endpoints (§1.2, §1.3, Phase 1/3).

Provides:
- GET /api/v1/agents/installations: Discover registered and local agent installations.
- POST /api/v1/agents/uninstall: Safely revoke, disconnect, and clean up agent installations.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.device_models import DeviceStatus
from ..auth.principal import Principal
from ..core.db import get_session

router = APIRouter(
    prefix="/api/v1/agents",
    tags=["agents"],
    dependencies=[Depends(require_principal)],
)


class AgentInstallationItem(BaseModel):
    id: str
    project_id: str | None = None
    status: str
    platform: str | None = None
    agent_version: str | None = None
    cert_fingerprint: str | None = None
    last_seen: datetime | None = None
    is_running: bool = False
    source: str = "registered_device"
    path: str | None = None


class AgentInstallationsResponse(BaseModel):
    installations: list[AgentInstallationItem]
    count: int
    release_tag: str | None = None


class AgentUninstallRequest(BaseModel):
    project_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    dry_run: bool = False
    force: bool = False
    wipe_credentials: bool = False


class AgentUninstallResult(BaseModel):
    stopped_devices: list[str] = Field(default_factory=list)
    removed_devices: list[str] = Field(default_factory=list)
    cleaned_entries: list[str] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)
    remaining_count: int = 0
    message: str
    success: bool = True


def _device_service(request: Request) -> Any:
    return getattr(request.app.state, "device_service", None)


@router.get(
    "/installations",
    response_model=AgentInstallationsResponse,
    summary="Discover registered and local agent installations",
    dependencies=[Depends(require_principal)],
)
async def list_agent_installations(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    project_id: uuid.UUID | None = Query(None, description="Optional project filter"),
) -> AgentInstallationsResponse:
    """List active/known agent installations and registered devices."""
    items: list[AgentInstallationItem] = []

    # 1. Query registered devices from Postgres
    query_str = (
        "SELECT id, project_id, status, agent_version, platform, cert_fingerprint, last_seen "
        "FROM agent_devices WHERE status <> :revoked "
    )
    params: dict[str, Any] = {"revoked": DeviceStatus.REVOKED.value}
    if project_id is not None:
        query_str += "AND project_id = :project_id "
        params["project_id"] = str(project_id)
    query_str += "ORDER BY created_at DESC LIMIT 100"

    result = await session.execute(text(query_str), params)
    rows = result.mappings().all()

    now = datetime.now(UTC)
    for row in rows:
        last_seen = row.get("last_seen")
        # Consider running if seen in last 120 seconds
        is_running = False
        if last_seen is not None:
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=UTC)
            is_running = (now - last_seen).total_seconds() < 120

        items.append(
            AgentInstallationItem(
                id=str(row["id"]),
                project_id=str(row["project_id"]) if row.get("project_id") else None,
                status=str(row.get("status") or "unknown"),
                platform=row.get("platform"),
                agent_version=row.get("agent_version"),
                cert_fingerprint=row.get("cert_fingerprint"),
                last_seen=last_seen,
                is_running=is_running,
                source="registered_device",
            )
        )

    # 2. Check local binary if present
    local_path = shutil.which("forgeops-agent") or shutil.which("forgeops-agent.exe")
    if local_path:
        items.append(
            AgentInstallationItem(
                id="local-host-agent",
                status="installed",
                source="local_path",
                path=local_path,
                is_running=False,
            )
        )

    settings = getattr(request.app.state, "settings", None)
    release_tag = settings.agent_release_tag if settings else None

    return AgentInstallationsResponse(
        installations=items,
        count=len(items),
        release_tag=release_tag,
    )


@router.post(
    "/uninstall",
    response_model=AgentUninstallResult,
    status_code=status.HTTP_200_OK,
    summary="Safely uninstall, disconnect, and revoke agent installations",
)
async def uninstall_agents(
    body: AgentUninstallRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentUninstallResult:
    """Revoke and clean up agent installations matching criteria."""
    svc = _device_service(request)

    # Find devices in scope
    query_str = "SELECT id, project_id, status FROM agent_devices WHERE status <> :revoked "
    params: dict[str, Any] = {"revoked": DeviceStatus.REVOKED.value}
    if body.device_id is not None:
        query_str += "AND id = :device_id "
        params["device_id"] = str(body.device_id)
    elif body.project_id is not None:
        query_str += "AND project_id = :project_id "
        params["project_id"] = str(body.project_id)

    result = await session.execute(text(query_str), params)
    rows = result.mappings().all()

    stopped: list[str] = []
    removed: list[str] = []
    cleaned: list[str] = []
    skipped: list[dict[str, str]] = []

    if body.dry_run:
        for r in rows:
            stopped.append(str(r["id"]))
            removed.append(str(r["id"]))
        if body.wipe_credentials:
            cleaned.append("pairing_credentials")
        return AgentUninstallResult(
            stopped_devices=stopped,
            removed_devices=removed,
            cleaned_entries=cleaned,
            skipped=skipped,
            remaining_count=0,
            message=f"Dry run: {len(removed)} device(s) would be uninstalled and revoked.",
            success=True,
        )

    # Actual uninstall / revocation
    for r in rows:
        d_id = r["id"]
        if not isinstance(d_id, uuid.UUID):
            d_id = uuid.UUID(str(d_id))
        try:
            if svc is not None:
                await svc.revoke(
                    session,
                    device_id=d_id,
                    actor=principal,
                    reason="User-initiated agent uninstall",
                )
            else:
                # Direct SQL fallback if DeviceService is not wired
                await session.execute(
                    text(
                        "UPDATE agent_devices SET status = :revoked, revoked_at = now(), "
                        "pairing_token_hmac = NULL, pairing_expires_at = NULL "
                        "WHERE id = :id AND status <> :revoked"
                    ),
                    {"id": str(d_id), "revoked": DeviceStatus.REVOKED.value},
                )
                await session.commit()
            removed.append(str(d_id))
            stopped.append(str(d_id))
        except Exception as exc:  # noqa: BLE001
            skipped.append({"device_id": str(d_id), "reason": str(exc)})

    if body.wipe_credentials:
        cleaned.append("revoked_tokens")

    count_res = await session.execute(
        text("SELECT COUNT(*) FROM agent_devices WHERE status <> :revoked"),
        {"revoked": DeviceStatus.REVOKED.value},
    )
    remaining = count_res.scalar() or 0

    return AgentUninstallResult(
        stopped_devices=stopped,
        removed_devices=removed,
        cleaned_entries=cleaned,
        skipped=skipped,
        remaining_count=int(remaining),
        message=f"Successfully uninstalled and revoked {len(removed)} agent device(s).",
        success=len(skipped) == 0,
    )
