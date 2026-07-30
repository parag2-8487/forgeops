# SPDX-License-Identifier: FSL-1.1-ALv2
"""AuditEvent (design.md §6.2, §6.3, §6.5 revision `0007`, §11.9).

Append-only and tamper-evident, enforced by the DATABASE rather than the app.
Three mechanisms, because any one alone is insufficient:

1. `seq BIGSERIAL` gives a total order per database, so a deletion leaves a gap.
2. `hash = sha256(canonical(payload) || prev_hash)` chains the records, so editing
   an old row invalidates every later hash — detectable without a second copy.
3. UPDATE, DELETE and TRUNCATE are REVOKED from the application role *and* raise
   in a trigger, so neither an ORM bug nor a stray SQL statement can rewrite
   history. Migrations run as a different role (§6.7).

`project_id` and `actor_user_id` are deliberately **not** foreign keys. An
immutable log that cascades away when a project is deleted is not an immutable
log. Referential integrity is traded for durability on purpose; the columns are
still indexed so the viewer can use them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    LargeBinary,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class AuditEvent(SQLModel, table=True):
    """§1.9's record. One row per governance transit, never updated."""

    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("id", name="uq_audit_events_id"),
        Index("ix_audit_project_created", "project_id", "created_at"),
        Index("ix_audit_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_resource", "resource_kind", "resource_id"),
    )

    seq: int | None = Field(
        default=None,
        sa_column=Column("seq", BigInteger, primary_key=True, autoincrement=True),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    project_id: uuid.UUID | None = Field(default=None, index=True)
    actor_user_id: uuid.UUID | None = Field(default=None)
    actor_device_id: uuid.UUID | None = Field(default=None)
    actor_kind: str = Field(max_length=16)
    action: str = Field(max_length=64)
    resource_kind: str = Field(max_length=64)
    resource_id: str | None = Field(default=None, max_length=128)
    # The "why" NFR-14 requires. Not nullable: a governance transit with no stated
    # reason is exactly the record that is useless six months later.
    reason: str = Field(max_length=1024)
    before_state: dict | None = Field(default=None, sa_column=Column("before_state", JSONB, nullable=True))
    after_state: dict | None = Field(default=None, sa_column=Column("after_state", JSONB, nullable=True))
    outcome: str = Field(max_length=32)
    trace_id: str | None = Field(default=None, max_length=32)
    prev_hash: bytes = Field(sa_column=Column("prev_hash", LargeBinary(32), nullable=False))
    hash: bytes = Field(sa_column=Column("hash", LargeBinary(32), nullable=False))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
