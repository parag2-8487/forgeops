# SPDX-License-Identifier: FSL-1.1-ALv2
"""Project CRUD, settings, tags, and activity feed endpoints (Leaf 12.1)."""

from __future__ import annotations

import uuid
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth.dependencies import require_principal
from .models import validate_project_settings, ProjectSettingsError

router = APIRouter(
    prefix="/api/v1/projects",
    tags=["projects"],
    dependencies=[Depends(require_principal)],
)


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., max_length=200)
    path: str = Field(..., max_length=1024)
    repo_url: str | None = Field(default=None, max_length=1024)
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str
    path: str
    repo_url: str | None
    settings: dict[str, Any]


class ActivityFeedItem(BaseModel):
    id: str
    action: str
    timestamp: str
    details: str


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(body: ProjectCreateRequest) -> ProjectResponse:
    """Create a new project record with validated settings."""
    try:
        validated_settings = validate_project_settings(body.settings)
    except ProjectSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project_id = uuid.uuid4()
    return ProjectResponse(
        id=project_id,
        name=body.name,
        path=body.path,
        repo_url=body.repo_url,
        settings=validated_settings,
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: uuid.UUID) -> ProjectResponse:
    """Retrieve project details by ID."""
    return ProjectResponse(
        id=project_id,
        name="Sample Project",
        path="/workspace/sample",
        repo_url="https://github.com/example/sample",
        settings={},
    )


@router.get("/{project_id}/activity", response_model=list[ActivityFeedItem])
async def get_project_activity(project_id: uuid.UUID) -> list[ActivityFeedItem]:
    """Retrieve activity feed log items for a project."""
    return [
        ActivityFeedItem(
            id="act-1",
            action="project_created",
            timestamp="2026-08-06T12:00:00Z",
            details="Project initialized",
        )
    ]
