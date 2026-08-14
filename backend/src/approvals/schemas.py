# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    ROLLED_BACK = "ROLLED_BACK"


class ChangeSetResponse(BaseModel):
    id: str
    project_id: str
    summary: str
    status: ApprovalStatus
    diff: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    approved_by: str | None = None
