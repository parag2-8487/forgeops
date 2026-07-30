# SPDX-License-Identifier: FSL-1.1-ALv2
"""Policies, evaluations and published bundles.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-30

Design: §6.1, §6.3, §6.5, §11.7, Appendix E criterion 7.

The bundle uniqueness rule is the interesting part. Exactly one bundle may be
active per scope, where a scope is a project or the global scope, while any number
of superseded bundles are retained — a device that pinned an old digest must still
be explainable. A plain `UNIQUE (project_id, active)` cannot express that, and it
would also collapse every inactive bundle into one row per project. Two PARTIAL
unique indexes do express it:

  * `uq_policy_bundles_one_active_per_project` — `WHERE active AND project_id IS NOT NULL`
  * `uq_policy_bundles_one_active_global`      — `WHERE active AND project_id IS NULL`

The second one exists because `UNIQUE (project_id)` treats NULLs as distinct, so a
single index would happily allow two active global bundles. That is exactly the case
`test_0005_policies.py` asserts against.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.policies.models import (
    EVALUATION_RESULTS,
    EVALUATION_SIDES,
    POLICY_ENGINES,
    in_list,
)

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("engine", sa.String(length=16), nullable=False),
        sa.Column("rego_rules", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_policies"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_policies_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "name", name="uq_policies_project_id_name"),
        sa.CheckConstraint(in_list("engine", POLICY_ENGINES), name="ck_policies_engine_allowed"),
    )
    op.create_index("ix_policies_project_id", "policies", ["project_id"])
    op.create_index("ix_policies_tenant_id", "policies", ["tenant_id"])

    op.create_table(
        "policy_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column("change_set_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=1024), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_policy_evaluations"),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["policies.id"],
            name="fk_policy_evaluations_policy_id_policies",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["change_sets.id"],
            name="fk_policy_evaluations_change_set_id_change_sets",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(in_list("result", EVALUATION_RESULTS), name="ck_policy_evaluations_result_allowed"),
        sa.CheckConstraint(in_list("side", EVALUATION_SIDES), name="ck_policy_evaluations_side_allowed"),
    )
    op.create_index("ix_policy_evaluations_policy_id", "policy_evaluations", ["policy_id"])
    op.create_index("ix_policy_evaluations_change_set_id", "policy_evaluations", ["change_set_id"])
    op.create_index("ix_policy_evaluations_tenant_id", "policy_evaluations", ["tenant_id"])

    op.create_table(
        "policy_bundles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(length=71), nullable=False),
        sa.Column("bundle", sa.LargeBinary(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_policy_bundles"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_policy_bundles_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("digest", name="uq_policy_bundles_digest"),
    )
    op.create_index("ix_policy_bundles_project_id", "policy_bundles", ["project_id"])
    op.create_index("ix_policy_bundles_tenant_id", "policy_bundles", ["tenant_id"])

    # One active bundle per project scope.
    op.execute(
        "CREATE UNIQUE INDEX uq_policy_bundles_one_active_per_project "
        "ON policy_bundles (project_id) WHERE active AND project_id IS NOT NULL"
    )
    # And one active GLOBAL bundle. A separate index is required because SQL treats
    # NULLs as distinct in a unique index, so the index above cannot constrain the
    # global scope at all: two rows with `project_id IS NULL` do not collide.
    # Within this index's filtered set `active` is always true, so uniqueness on
    # that column admits exactly one row. An expression index on
    # `(project_id IS NULL)` would work too, but Alembic renders expression indexes
    # as opaque textual elements, so `alembic check` could never confirm that the
    # model and the database agree about it — and task 5.9 requires exactly that.
    op.execute(
        "CREATE UNIQUE INDEX uq_policy_bundles_one_active_global "
        "ON policy_bundles (active) WHERE active AND project_id IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_policy_bundles_one_active_global")
    op.execute("DROP INDEX IF EXISTS uq_policy_bundles_one_active_per_project")
    op.drop_index("ix_policy_bundles_tenant_id", table_name="policy_bundles")
    op.drop_index("ix_policy_bundles_project_id", table_name="policy_bundles")
    op.drop_table("policy_bundles")
    op.drop_index("ix_policy_evaluations_tenant_id", table_name="policy_evaluations")
    op.drop_index("ix_policy_evaluations_change_set_id", table_name="policy_evaluations")
    op.drop_index("ix_policy_evaluations_policy_id", table_name="policy_evaluations")
    op.drop_table("policy_evaluations")
    op.drop_index("ix_policies_tenant_id", table_name="policies")
    op.drop_index("ix_policies_project_id", table_name="policies")
    op.drop_table("policies")
