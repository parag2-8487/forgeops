# SPDX-License-Identifier: FSL-1.1-ALv2
"""AgentDevice SQLModel table (design.md §6.2, §6.3, §6.5 revision `0002`).

PRD §7 lists `agent_devices (id, project_id, pairing_token, device_token,
last_seen)`. Storing either token in plaintext would make a database read
equivalent to a stolen credential, so both are stored as HMACs under a server
pepper and the column names say `_hmac` so nobody can mistake them for values.

`envelope_key_enc` is the exception, and the reason is worth stating: the backend
must *use* that key to sign command envelopes, so it has to be recoverable. It is
therefore encrypted (AES-256-GCM under an app-level key from the secret store)
rather than hashed. Encryption where hashing would do is a downgrade, so the
asymmetry is deliberate and confined to this one column.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    LargeBinary,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel


class DeviceStatus(StrEnum):
    """§3.7's state machine, expressed as data."""

    PENDING = "pending"
    ACTIVE = "active"
    POLICY_STALE = "policy_stale"
    REVOKED = "revoked"
    ABANDONED = "abandoned"


class AgentDevice(SQLModel, table=True):
    """PRD D1 `agent_devices`, with the columns the real pairing flow needs."""

    __tablename__ = "agent_devices"
    __table_args__ = (
        UniqueConstraint("cert_serial", name="uq_agent_devices_cert_serial"),
        Index("ix_agent_devices_project_status", "project_id", "status"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    status: DeviceStatus = Field(
        sa_column=Column(
            "status",
            # See the note in auth/models.py: without `values_callable` SQLAlchemy
            # would persist "PENDING" where §3.7's state machine says "pending".
            SAEnum(DeviceStatus, name="device_status", values_callable=lambda e: [m.value for m in e]),
            nullable=False,
        )
    )
    pairing_token_hmac: bytes | None = Field(
        default=None, sa_column=Column("pairing_token_hmac", LargeBinary(32), nullable=True)
    )
    device_token_hmac: bytes | None = Field(
        default=None, sa_column=Column("device_token_hmac", LargeBinary(32), nullable=True)
    )
    envelope_key_enc: bytes | None = Field(
        default=None, sa_column=Column("envelope_key_enc", LargeBinary, nullable=True)
    )
    cert_serial: str | None = Field(default=None, max_length=64)
    cert_fingerprint: str | None = Field(default=None, max_length=95)  # sha256 colon-hex
    agent_version: str = Field(max_length=64)
    platform: str = Field(max_length=64)
    policy_bundle_digest: str | None = Field(default=None, max_length=71)  # "sha256:" + 64
    # Mirror of the Redis high-water mark, kept for forensics after a Redis flush.
    # Redis remains authoritative for replay rejection (§7.6); this column is
    # evidence, never the decision.
    last_seq: int = Field(
        default=0,
        sa_column=Column("last_seq", BigInteger, nullable=False, server_default=text("0")),
    )
    pairing_expires_at: datetime | None = Field(
        default=None, sa_column=Column("pairing_expires_at", DateTime(timezone=True), nullable=True)
    )
    cert_not_after: datetime | None = Field(
        default=None, sa_column=Column("cert_not_after", DateTime(timezone=True), nullable=True)
    )
    last_seen: datetime | None = Field(
        default=None, sa_column=Column("last_seen", DateTime(timezone=True), nullable=True)
    )
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column("revoked_at", DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
