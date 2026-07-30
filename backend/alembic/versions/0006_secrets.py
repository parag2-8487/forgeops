# SPDX-License-Identifier: FSL-1.1-ALv2
"""Secrets, with §6.6's Phase 1 environment resolution.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-30

Design: §6.3, §6.5, §6.6, §17.1 D-50.

`environment` is constrained TEXT and there is no foreign key, because the table it
would reference — `environments` — belongs to PRD D4, which is Phase 2. The text is
constrained to exactly the four names Phase 2 will create, which makes Phase 2's
backfill a deterministic four-value map rather than a data-cleaning exercise.

The exclusivity constraint is the other half of D-50: exactly one of
`infisical_path` and `encrypted_value` is non-null. With `SECRET_BACKEND=infisical`
the row holds only the path, so this database is not a second copy of every secret;
`encrypted_value` exists for the local development backend alone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.secrets.models import EXCLUSIVE_STORAGE_SQL, SECRET_ENVIRONMENTS, in_list

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("infisical_path", sa.String(length=1024), nullable=True),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=True),
        sa.Column("rotation_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_secrets"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_secrets_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "environment", "key", name="uq_secrets_project_id_environment_key"),
        sa.CheckConstraint(in_list("environment", SECRET_ENVIRONMENTS), name="ck_secrets_environment_allowed"),
        sa.CheckConstraint(EXCLUSIVE_STORAGE_SQL, name="ck_secrets_exactly_one_storage"),
    )
    op.create_index("ix_secrets_project_id", "secrets", ["project_id"])
    op.create_index("ix_secrets_tenant_id", "secrets", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_secrets_tenant_id", table_name="secrets")
    op.drop_index("ix_secrets_project_id", table_name="secrets")
    op.drop_table("secrets")
