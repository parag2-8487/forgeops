# SPDX-License-Identifier: FSL-1.1-ALv2
"""ArgoCD repository events. Phase 2 2.7, revision 0032.

EVERY MIGRATION NEEDS ITS MODEL. Three tables once had migrations and no models, and `alembic check`
proposed dropping all three -- so this exists to keep autogenerate's view of the schema equal to the
migrations' view. The webhook route writes through raw SQL because it holds a payload it does not
interpret, and an ORM round trip would add nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ArgocdRepositoryEvent(SQLModel, table=True):
    """One recorded repository change. NOT a sync: see `argocd/routes.py` for why."""

    __tablename__ = "argocd_repository_events"
    __table_args__ = (Index("ix_argocd_repository_events_repository_received_at", "repository", "received_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    repository: str = Field(max_length=1024, index=True)
    # May be empty: a forge's ping event names a repository and no commit, and recording it still proves
    # the webhook is wired -- which is otherwise unanswerable.
    revision: str = Field(default="", max_length=255)
    payload: dict[str, Any] = Field(sa_column=Column("payload", JSONB, nullable=False))
    received_at: datetime = Field(
        sa_column=Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )
