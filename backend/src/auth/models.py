# SPDX-License-Identifier: FSL-1.1-ALv2
"""User and Session SQLModel tables (design.md §6.2, §6.3, §6.5 revision `0002`).

Two rules on this file are load-bearing and easy to lose in a refactor:

`email` is `CITEXT`, not `VARCHAR`. Authentik lower-cases nothing, so `A@b.com`
and `a@b.com` arrive as distinct strings; a `VARCHAR` unique index would happily
create two accounts for one human, and the second one would be invisible to the
first. The extension is created by `0002` before the column exists.

`sessions.refresh_token_hmac` is an HMAC, never the token and never a reversible
encryption of it. A stolen database dump must not yield usable refresh tokens.
The column name says `hmac` so a future reader cannot mistake it for the value.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Column,
    DateTime,
    LargeBinary,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import CITEXT
from sqlmodel import Field, SQLModel


class UserRole(StrEnum):
    """The three Phase 1 roles (§1.11). Cerbos resolves resource-scoped rules;
    this enum is the coarse role the token carries."""

    ADMIN = "admin"
    DEVELOPER = "developer"
    VIEWER = "viewer"


class User(SQLModel, table=True):
    """PRD D1 `users`."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("idp_subject", name="uq_users_idp_subject"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # D-35 seam: still nullable, still no RLS policy in Phase 1.
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    email: str = Field(sa_column=Column("email", CITEXT, nullable=False, unique=True))
    name: str = Field(max_length=200)
    # `values_callable` is not decoration. SQLAlchemy persists a Python enum's
    # *names* by default, so without it this column would store "ADMIN" while
    # every authority — the ERD, the token claim, Cerbos, the Rego policies —
    # says "admin". The mismatch would only surface as an authorisation failure.
    role: UserRole = Field(
        sa_column=Column(
            "role",
            SAEnum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
            nullable=False,
        )
    )
    # The join key to Authentik. Email is mutable there; `sub` is not.
    idp_subject: str = Field(max_length=255)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )
    )


class Session(SQLModel, table=True):
    """PRD D1 `sessions`. Holds an HMAC of the refresh token, never the token."""

    __tablename__ = "sessions"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    refresh_token_hmac: bytes = Field(sa_column=Column("refresh_token_hmac", LargeBinary(32), nullable=False))
    idp_session_id: str | None = Field(default=None, max_length=255)
    expires_at: datetime = Field(sa_column=Column("expires_at", DateTime(timezone=True), nullable=False, index=True))
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column("revoked_at", DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
