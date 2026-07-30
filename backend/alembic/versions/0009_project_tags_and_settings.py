# SPDX-License-Identifier: FSL-1.1-ALv2
"""project_tags, and the settings key contract.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-30

Design: §6.5, §11.3.

`projects.settings` stays JSONB and the key validation stays in Python, in
`src/projects/models.py::validate_project_settings`. Expressing it as a check
constraint per key would make every new setting a migration, and expressing it as
one constraint over a JSONB document would be an unreadable predicate that still
could not say "must be a list of strings". What the schema *can* usefully assert is
that the column is a JSON object rather than an array or a scalar, so that is what
it asserts — a real narrowing that costs nothing and cannot rot.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_tags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("tag", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_project_tags"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_tags_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "tag", name="uq_project_tags_project_id_tag"),
    )
    op.create_index("ix_project_tags_project_id", "project_tags", ["project_id"])

    op.create_check_constraint(
        "ck_projects_settings_is_object",
        "projects",
        "jsonb_typeof(settings) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_projects_settings_is_object", "projects", type_="check")
    op.drop_index("ix_project_tags_project_id", table_name="project_tags")
    op.drop_table("project_tags")
