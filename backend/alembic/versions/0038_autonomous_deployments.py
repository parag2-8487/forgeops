# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous deployment orchestrator schema: runs, stages, logs, and outbox.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-10

WHY THESE TABLES EXIST
----------------------
Autonomous deployment orchestrates multi-stage verification (G1-G7) and multi-target delivery
(Docker, GitHub, Vercel) with leased worker fencing and monotonic progress tracking.

1. `autonomous_deployments`:
   Authoritative state machine for deployment runs and attempt chains.
   Fenced execution via `worker_id`, `fence_token`, and `lease_expires_at` prevents split-brain
   concurrent workers. Idempotency guarantees safe submission retries.

2. `autonomous_deployment_stages`:
   Operational steps and gate verdicts (G1-G7). Each stage records deterministic position, status,
   timing, and structured execution metadata.

3. `autonomous_deployment_logs`:
   Durable, ordered execution log lines with per-run monotonically increasing `log_seq`.

4. `autonomous_deployment_outbox`:
   Transactional outbox for reliable SSE and WebSocket event streaming relay, decoupling state
   transitions from pub/sub delivery.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_STATUSES = (
    "pending",
    "running",
    "cancelling",
    "cancelled",
    "succeeded",
    "failed",
    "rolled_back",
)

STAGE_STATUSES = (
    "pending",
    "waiting",
    "running",
    "cancelling",
    "cancelled",
    "succeeded",
    "failed",
    "skipped",
    "rolled_back",
)

LOG_LEVELS = ("INFO", "WARN", "ERROR")
OUTBOX_STATUSES = ("pending", "published")


def upgrade() -> None:
    # 1. autonomous_deployments
    op.create_table(
        "autonomous_deployments",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "parent_run_id",
            sa.Uuid(),
            sa.ForeignKey("autonomous_deployments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("strategy", sa.String(length=32), nullable=False),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_stage", sa.String(length=64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("primary_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("compensation_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Worker Fencing and Lease Custody
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("fence_token", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        # Dispatch and Lifecycle Tracking
        sa.Column("dispatch_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("dispatch_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        # Atomic Sequence Allocators
        sa.Column("log_sequence_counter", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outbox_sequence_counter", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_proj_idempotency"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in RUN_STATUSES) + ")",
            name="chk_run_status",
        ),
        sa.CheckConstraint("progress_pct >= 0 AND progress_pct <= 100", name="chk_progress_range"),
    )
    op.create_index(
        "idx_auto_deploy_proj_status",
        "autonomous_deployments",
        ["project_id", "status"],
    )
    op.create_index(
        "idx_auto_deploy_lease",
        "autonomous_deployments",
        ["status", "lease_expires_at"],
    )

    # 2. autonomous_deployment_stages
    op.create_table(
        "autonomous_deployment_stages",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("autonomous_deployments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_name", sa.String(length=64), nullable=False),
        sa.Column("gate_id", sa.String(length=16), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "stage_name", name="uq_run_stage"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STAGE_STATUSES) + ")",
            name="chk_stage_status",
        ),
    )
    op.create_index(
        "idx_auto_deploy_stages_run",
        "autonomous_deployment_stages",
        ["run_id", "position"],
    )

    # 3. autonomous_deployment_logs
    op.create_table(
        "autonomous_deployment_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("autonomous_deployments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_name", sa.String(length=64), nullable=False),
        sa.Column("log_seq", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="INFO"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "log_seq", name="uq_run_log_seq"),
        sa.CheckConstraint(
            "level IN (" + ", ".join(f"'{lvl}'" for lvl in LOG_LEVELS) + ")",
            name="chk_log_level",
        ),
    )
    op.create_index(
        "idx_auto_deploy_logs_query",
        "autonomous_deployment_logs",
        ["run_id", "log_seq"],
    )

    # 4. autonomous_deployment_outbox
    op.create_table(
        "autonomous_deployment_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("autonomous_deployments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "event_seq", name="uq_run_outbox_seq"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in OUTBOX_STATUSES) + ")",
            name="chk_outbox_status",
        ),
    )
    op.create_index(
        "idx_auto_deploy_outbox_pending",
        "autonomous_deployment_outbox",
        ["status", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_auto_deploy_outbox_pending", table_name="autonomous_deployment_outbox")
    op.drop_table("autonomous_deployment_outbox")
    op.drop_index("idx_auto_deploy_logs_query", table_name="autonomous_deployment_logs")
    op.drop_table("autonomous_deployment_logs")
    op.drop_index("idx_auto_deploy_stages_run", table_name="autonomous_deployment_stages")
    op.drop_table("autonomous_deployment_stages")
    op.drop_index("idx_auto_deploy_lease", table_name="autonomous_deployments")
    op.drop_index("idx_auto_deploy_proj_status", table_name="autonomous_deployments")
    op.drop_table("autonomous_deployments")
