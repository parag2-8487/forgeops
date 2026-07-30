# SPDX-License-Identifier: FSL-1.1-ALv2
"""Change sets, items, validations, approvals and rollback handles.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30

Design: §6.2, §6.3, §6.5, §3.6.

The status and action check constraints are generated from the tuples in
`src/governance/models.py`, not written out here as literals. A new lifecycle state
therefore cannot be added to the application without the database learning about it
in the same commit — which is the only way the two stay in step.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.governance.models import (
    APPROVAL_STATUSES,
    CHANGE_ITEM_ACTIONS,
    CHANGE_SET_ORIGINS,
    CHANGE_SET_STATUSES,
    in_list,
)

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("generation_run_id", sa.Uuid(), nullable=True),
        sa.Column("blast_radius_score", sa.Integer(), nullable=False),
        sa.Column("blast_radius_verdict", sa.String(length=32), nullable=False),
        sa.Column("policy_bundle_digest", sa.String(length=71), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_change_sets"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_change_sets_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_change_sets_created_by_users",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(in_list("status", CHANGE_SET_STATUSES), name="ck_change_sets_status_allowed"),
        sa.CheckConstraint(in_list("origin", CHANGE_SET_ORIGINS), name="ck_change_sets_origin_allowed"),
    )
    op.create_index("ix_change_sets_project_id", "change_sets", ["project_id"])
    op.create_index("ix_change_sets_tenant_id", "change_sets", ["tenant_id"])
    op.create_index("ix_change_sets_generation_run_id", "change_sets", ["generation_run_id"])
    op.create_index("ix_change_sets_project_status", "change_sets", ["project_id", "status"])

    op.create_table(
        "change_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("change_set_id", sa.Uuid(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("old_content", sa.Text(), nullable=True),
        sa.Column("new_content", sa.Text(), nullable=True),
        sa.Column("old_hash", sa.String(length=64), nullable=True),
        sa.Column("new_hash", sa.String(length=64), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_change_items"),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["change_sets.id"],
            name="fk_change_items_change_set_id_change_sets",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("change_set_id", "ordinal", name="uq_change_items_change_set_id_ordinal"),
        sa.CheckConstraint(in_list("action", CHANGE_ITEM_ACTIONS), name="ck_change_items_action_allowed"),
    )
    op.create_index("ix_change_items_change_set_id", "change_items", ["change_set_id"])

    op.create_table(
        "validations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("change_item_id", sa.Uuid(), nullable=False),
        sa.Column("validator", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_validations"),
        sa.ForeignKeyConstraint(
            ["change_item_id"],
            ["change_items.id"],
            name="fk_validations_change_item_id_change_items",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_validations_change_item_id", "validations", ["change_item_id"])
    op.create_index("ix_validations_change_item_id_iteration", "validations", ["change_item_id", "iteration"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("change_set_id", sa.Uuid(), nullable=False),
        sa.Column("approver_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["change_sets.id"],
            name="fk_approvals_change_set_id_change_sets",
            ondelete="CASCADE",
        ),
        # RESTRICT: deleting a user must not erase the record of what they approved.
        sa.ForeignKeyConstraint(
            ["approver_id"],
            ["users.id"],
            name="fk_approvals_approver_id_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(in_list("status", APPROVAL_STATUSES), name="ck_approvals_status_allowed"),
    )
    op.create_index("ix_approvals_change_set_id", "approvals", ["change_set_id"])
    op.create_index("ix_approvals_approver_id", "approvals", ["approver_id"])

    op.create_table(
        "rollback_handles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("change_set_id", sa.Uuid(), nullable=False),
        sa.Column("backup_manifest", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("agent_device_id", sa.String(length=64), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_rollback_handles"),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["change_sets.id"],
            name="fk_rollback_handles_change_set_id_change_sets",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("change_set_id", name="uq_rollback_handles_change_set_id"),
    )


def downgrade() -> None:
    op.drop_table("rollback_handles")
    op.drop_index("ix_approvals_approver_id", table_name="approvals")
    op.drop_index("ix_approvals_change_set_id", table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_validations_change_item_id_iteration", table_name="validations")
    op.drop_index("ix_validations_change_item_id", table_name="validations")
    op.drop_table("validations")
    op.drop_index("ix_change_items_change_set_id", table_name="change_items")
    op.drop_table("change_items")
    op.drop_index("ix_change_sets_project_status", table_name="change_sets")
    op.drop_index("ix_change_sets_generation_run_id", table_name="change_sets")
    op.drop_index("ix_change_sets_tenant_id", table_name="change_sets")
    op.drop_index("ix_change_sets_project_id", table_name="change_sets")
    op.drop_table("change_sets")
