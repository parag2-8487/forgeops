# SPDX-License-Identifier: FSL-1.1-ALv2
"""Command Center history. Phase 2 2.5, revision 0036.

EVERY MIGRATION NEEDS ITS MODEL, so `alembic check` sees the same schema the migrations built. The route
writes through raw SQL because it holds no object worth mapping -- one append per interpretation.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Text, func
from sqlmodel import Field, SQLModel

#: What happened to an utterance. `refused` is first-class: a declined command must be visible, or
#: "nothing happened" is indistinguishable from a broken button.
COMMAND_OUTCOMES: tuple[str, ...] = ("interpreted", "dispatched", "read", "refused")


class CommandHistory(SQLModel, table=True):
    """One thing a user asked for, and what was made of it."""

    __tablename__ = "command_history"
    __table_args__ = (
        Index("ix_command_history_session", "session_key", "created_at"),
        Index("ix_command_history_created_at", "created_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    utterance: str = Field(default="", sa_column=Column("utterance", Text, nullable=False))
    intent: str = Field(default="", max_length=64)
    outcome: str = Field(max_length=16)
    detail: str = Field(default="", sa_column=Column("detail", Text, nullable=False))
    session_key: str = Field(default="", max_length=128)
    created_by: uuid.UUID | None = Field(default=None)
    created_at: datetime = Field(
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())
    )
