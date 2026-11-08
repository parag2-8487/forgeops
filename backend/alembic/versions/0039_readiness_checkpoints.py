# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous deployment readiness checkpoints schema.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-12

WHY THIS TABLE EXISTS
----------------------
Readiness checkpoints capture immutable, strategy-aware project health evaluations
at key milestones:
1. Initial baseline (R0) after source preparation and before G1.
2. Post-generation (R1) after G2 artifact synthesis and before G3.
3. Recovery re-evaluations (R_recN) versioned without overwriting R0 or R1.

The unique constraint `uq_run_checkpoint_version` on `(run_id, checkpoint_type, assessment_version)`
guarantees idempotency and prevents concurrent duplicates.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKPOINT_TYPES = ("initial_r0", "post_generation_r1", "recovery_recheck")
CHECKPOINT_STATUSES = ("completed", "failed")


def upgrade() -> None:
    op.create_table(
        "deployment_readiness_checkpoints",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("autonomous_deployments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("checkpoint_type", sa.String(length=32), nullable=False),
        sa.Column("assessment_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("max_score", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("score_delta", sa.Integer(), nullable=True),
        sa.Column("selected_strategy", sa.String(length=32), nullable=False),
        sa.Column("detected_stack", sa.String(length=64), nullable=False),
        sa.Column(
            "snapshot_identity",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "dimension_scores",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "dimension_explanations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "blockers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "artifacts_detected",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "resolved_blockers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "new_blockers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("assessment_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "checkpoint_type", "assessment_version", name="uq_run_checkpoint_version"),
        sa.CheckConstraint(
            "checkpoint_type IN (" + ", ".join(f"'{c}'" for c in CHECKPOINT_TYPES) + ")",
            name="chk_readiness_checkpoint_type",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in CHECKPOINT_STATUSES) + ")",
            name="chk_readiness_status",
        ),
    )
    op.create_index(
        "idx_readiness_run_lookup",
        "deployment_readiness_checkpoints",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_readiness_run_lookup", table_name="deployment_readiness_checkpoints")
    op.drop_table("deployment_readiness_checkpoints")
