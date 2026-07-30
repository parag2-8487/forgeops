# SPDX-License-Identifier: FSL-1.1-ALv2
"""Secret metadata (design.md §6.2, §6.5 `0006`, §6.6, §11.8, §17.1 D-50).

The table holds *metadata and a pointer*, not a second copy of every secret.

`environment` is constrained TEXT, not a foreign key. PRD D5 writes
`environment_id`, but `environments` belongs to PRD D4, which is Phase 2, and a
nullable foreign key to a table that does not exist is a broken reference rather
than a seam (D-50). The text is constrained to exactly the four names Phase 2 will
create, which is what makes Phase 2's backfill deterministic.

Exactly one of `infisical_path` and `encrypted_value` is non-null, enforced by a
check constraint. With `SECRET_BACKEND=infisical` the ciphertext lives in Infisical
and the row holds only the path; `encrypted_value` exists for the local development
backend, where values are sealed with AES-256-GCM under a key from the environment.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    LargeBinary,
    UniqueConstraint,
    func,
)
from sqlmodel import Field, SQLModel

#: The four names Phase 2's `environments` table will create (D-50). Constraining
#: the text to exactly these is what makes the Phase 2 backfill a four-value map.
SECRET_ENVIRONMENTS: tuple[str, ...] = ("dev", "test", "staging", "prod")


def in_list(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


#: Exactly one storage location, never both and never neither.
EXCLUSIVE_STORAGE_SQL = (
    "(infisical_path IS NOT NULL AND encrypted_value IS NULL) OR "
    "(infisical_path IS NULL AND encrypted_value IS NOT NULL)"
)


class Secret(SQLModel, table=True):
    """PRD D5 `secrets`, with §6.6's Phase 1 environment resolution."""

    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("project_id", "environment", "key", name="uq_secrets_project_id_environment_key"),
        CheckConstraint(in_list("environment", SECRET_ENVIRONMENTS), name="ck_secrets_environment_allowed"),
        CheckConstraint(EXCLUSIVE_STORAGE_SQL, name="ck_secrets_exactly_one_storage"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    environment: str = Field(max_length=16)
    key: str = Field(max_length=255)
    infisical_path: str | None = Field(default=None, max_length=1024)
    encrypted_value: bytes | None = Field(default=None, sa_column=Column("encrypted_value", LargeBinary, nullable=True))
    rotation_date: datetime | None = Field(
        default=None, sa_column=Column("rotation_date", DateTime(timezone=True), nullable=True)
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
