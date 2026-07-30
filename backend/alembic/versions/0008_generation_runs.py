# SPDX-License-Identifier: FSL-1.1-ALv2
"""generation_runs, with the iteration bound expressed in the schema.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-30

Design: §6.1, §6.2, §6.5, §11.5, §3.8, Q-08.

`iterations_used BETWEEN 0 AND 3` is the §3.8 bound stated a third time. It is
already in the type — `generation_max_iterations` is `Literal[3]`, so no environment
variable can raise it — and in Q-08's termination property. Three independent
expressions of one invariant is what makes a regression in any single layer
impossible to ship quietly, which is worth one check constraint.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.generation.models import (
    GENERATION_STATUSES,
    MAX_GENERATION_ITERATIONS,
    SERVED_FROM,
    in_list,
)

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("iterations_used", sa.Integer(), nullable=False),
        sa.Column("served_from", sa.String(length=16), nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=False),
        sa.Column("endpoint_id", sa.String(length=100), nullable=True),
        sa.Column("rubric", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("retrieval", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_generation_runs"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_generation_runs_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_generation_runs_requested_by_users",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            f"iterations_used BETWEEN 0 AND {MAX_GENERATION_ITERATIONS}",
            name="ck_generation_runs_iterations_bounded",
        ),
        sa.CheckConstraint(in_list("status", GENERATION_STATUSES), name="ck_generation_runs_status_allowed"),
        sa.CheckConstraint(in_list("served_from", SERVED_FROM), name="ck_generation_runs_served_from_allowed"),
    )
    op.create_index("ix_generation_runs_project_id", "generation_runs", ["project_id"])
    op.create_index("ix_generation_runs_tenant_id", "generation_runs", ["tenant_id"])
    op.create_index(
        "ix_generation_runs_project_id_created_at",
        "generation_runs",
        ["project_id", "created_at"],
    )

    # `change_sets.generation_run_id` was created in `0004` without its foreign key,
    # because the table it references does not exist until this revision. The ERD
    # (§6.2) makes it a real reference, so it is added here rather than left as a
    # bare UUID: an unenforced "FK" is how the dangling `environment_id` reference
    # D-50 had to resolve came about in the first place.
    op.create_foreign_key(
        "fk_change_sets_generation_run_id_generation_runs",
        "change_sets",
        "generation_runs",
        ["generation_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_change_sets_generation_run_id_generation_runs", "change_sets", type_="foreignkey")
    op.drop_index("ix_generation_runs_project_id_created_at", table_name="generation_runs")
    op.drop_index("ix_generation_runs_tenant_id", table_name="generation_runs")
    op.drop_index("ix_generation_runs_project_id", table_name="generation_runs")
    op.drop_table("generation_runs")
