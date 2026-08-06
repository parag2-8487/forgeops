# SPDX-License-Identifier: Apache-2.0
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime, timezone


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
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    approved_by: Optional[str] = None
