# SPDX-License-Identifier: FSL-1.1-ALv2
"""Project SQLModel table (design.md §6.2).

Exactly one table in this domain for Phase 0. The rest of D1 (users, teams,
sessions, agent_devices) belongs to the identity/auth domain excluded with
authentication.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Project(SQLModel, table=True):
    """Minimal project record for Phase 0.

    The only D1 table; identity/auth tables are Phase 1.
    """

    __tablename__ = "projects"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Seam for Phase 1 PostgreSQL RLS. Nullable now; NOT NULL + policies arrive
    # in the Phase 1 migration so there is no backfill of live rows later.
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    name: str = Field(max_length=200, index=True)
    path: str = Field(max_length=1024)
    repo_url: str | None = Field(default=None, max_length=1024)
    settings: dict = Field(
        default_factory=dict,
        sa_column=Column("settings", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )
    )
