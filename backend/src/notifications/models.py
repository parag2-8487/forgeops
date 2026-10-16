# SPDX-License-Identifier: FSL-1.1-ALv2
"""Notification tables as SQLModel declarations. §2.6.

Present because `alembic check` compares the migration graph against model metadata: revision `0028` created
two tables and, without these, autogenerate proposed DROPPING both — a migration with no model looks exactly
like a table somebody forgot to remove. The same trap caught `environments`, `environment_variables` and
`deployments` in an earlier pass.

Every CHECK the migration installs is declared here too, so the two cannot drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from .service import NOTIFICATION_CHANNELS, NOTIFICATION_KINDS


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class Notification(SQLModel, table=True):
    """One raised notification and what became of each delivery attempt."""

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(_in_list("kind", NOTIFICATION_KINDS), name="ck_notifications_kind"),
        Index("ix_notifications_project_created", "project_id", "created_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    )
    tenant_id: uuid.UUID | None = Field(default=None)
    kind: str = Field(max_length=32)
    subject: str = Field(max_length=200)
    body: str = Field(sa_column=Column("body", Text(), nullable=False))
    resource_kind: str | None = Field(default=None, max_length=32)
    resource_id: str | None = Field(default=None, max_length=64)
    #: A map from channel to `{delivered, detail}`. A map rather than a status column because one
    #: notification can succeed on Slack and be rejected by SMTP, and a column would hide one of them.
    delivery: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    read_at: datetime | None = Field(default=None, sa_column=Column("read_at", DateTime(timezone=True), nullable=True))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )


class NotificationPreference(SQLModel, table=True):
    """One user's setting for one channel and one kind."""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        CheckConstraint(_in_list("channel", NOTIFICATION_CHANNELS), name="ck_notification_preferences_channel"),
        CheckConstraint(_in_list("kind", NOTIFICATION_KINDS), name="ck_notification_preferences_kind"),
        # An enabled channel with nowhere to deliver is a setting that silently does nothing.
        CheckConstraint(
            "channel = 'in_app' OR NOT enabled OR target IS NOT NULL",
            name="ck_notification_preferences_target_present",
        ),
        UniqueConstraint("user_id", "project_id", "channel", "kind", name="uq_notification_preference"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID
    project_id: uuid.UUID = Field(
        sa_column=Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    )
    channel: str = Field(sa_column=Column("channel", String(16), nullable=False))
    kind: str = Field(max_length=32)
    enabled: bool = Field(default=True)
    target: str | None = Field(default=None, max_length=500)


__all__ = ["Notification", "NotificationPreference"]
