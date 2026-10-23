# SPDX-License-Identifier: FSL-1.1-ALv2
"""The notification HTTP surface. §2.6.

Reads and preference writes only — a notification is RAISED by whatever happened (a deployment settling, a
policy refusal), never by a client asking for one. There is deliberately no `POST /notifications`: a route
that let a caller send arbitrary messages to a project's Slack channel would be a spam endpoint with
authentication.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import problem
from .service import NOTIFICATION_CHANNELS, NOTIFICATION_KINDS, NotificationService

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/notifications",
    tags=["notifications"],
    dependencies=[Depends(require_principal)],
)


def _service(request: Request) -> NotificationService:
    return request.app.state.notification_service  # type: ignore[no-any-return]


class PreferenceRequest(BaseModel):
    """One user's setting for one channel and one kind."""

    channel: Literal["in_app", "slack", "discord", "email"]
    kind: Literal["deploy_completed", "deploy_failed", "policy_violated", "approval_required", "self_healed"]
    enabled: bool = True
    #: A webhook URL or an address. Required when enabling anything but `in_app` — an enabled channel with
    #: nowhere to deliver is a setting that silently does nothing.
    target: str | None = Field(default=None, max_length=500)


@router.get("", summary="This project's notifications, newest first")
async def list_notifications(
    project_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> dict[str, Any]:
    """Each row carries its per-channel delivery outcome, so the bell can show what did NOT get through."""
    records = await _service(request).list_for_project(session, project_id=project_id, limit=limit)
    return {
        "notifications": [
            {
                "id": str(record.id),
                "kind": record.kind,
                "subject": record.subject,
                "body": record.body,
                "resource_kind": record.resource_kind,
                "resource_id": record.resource_id,
                "delivery": record.delivery,
                "read_at": record.read_at,
                "created_at": record.created_at,
            }
            for record in records
        ],
        "unread": sum(1 for record in records if record.read_at is None),
        "kinds": list(NOTIFICATION_KINDS),
    }


@router.post("/{notification_id}/read", summary="Mark one notification read")
async def mark_read(
    project_id: uuid.UUID,
    notification_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    try:
        record = await _service(request).mark_read(session, notification_id=notification_id)
    except LookupError as error:
        raise problem("notification-absent", detail=str(error)) from error
    if record.project_id != project_id:
        raise problem(
            "resource-absent",
            detail=f"notification {notification_id} does not belong to project {project_id}",
        )
    await session.commit()
    return {"id": str(record.id), "read_at": record.read_at}


@router.get("/preferences", summary="This user's channel preferences for this project")
async def read_preferences(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """`target_configured` rather than the target itself: a webhook URL is a credential."""
    if principal.user_id is None:
        raise problem(
            "notification-preference-invalid",
            detail="notification preferences belong to a user, and this principal is not one",
        )
    return {
        "preferences": await _service(request).preferences(session, user_id=principal.user_id, project_id=project_id),
        "channels": list(NOTIFICATION_CHANNELS),
        "kinds": list(NOTIFICATION_KINDS),
    }


@router.put("/preferences", summary="Set one channel-and-kind preference")
async def set_preference(
    project_id: uuid.UUID,
    body: PreferenceRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    if principal.user_id is None:
        raise problem(
            "notification-preference-invalid",
            detail="notification preferences belong to a user, and this principal is not one",
        )
    try:
        await _service(request).set_preference(
            session,
            user_id=principal.user_id,
            project_id=project_id,
            channel=body.channel,
            kind=body.kind,
            enabled=body.enabled,
            target=body.target,
        )
    except ValueError as error:
        raise problem("notification-preference-invalid", detail=str(error)) from error
    await session.commit()
    return {"channel": body.channel, "kind": body.kind, "enabled": body.enabled}
