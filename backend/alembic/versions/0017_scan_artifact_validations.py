# SPDX-License-Identifier: FSL-1.1-ALv2
"""scan artifact validations

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-05

WHY A NEW TABLE RATHER THAN A COLUMN ON `analysis_reports`.

The existing `validations` table is keyed by `change_item_id`, so it can only ever describe a file this
system GENERATED. It had no rows. A file the user wrote themselves has no change item and never will, so
there was nowhere to record that `docker compose config` rejects their compose file — and the readiness
score fell back to asking whether the path existed.

These rows describe the user's OWN artifacts, are replaced wholesale on every full scan, and are scoped by
project rather than by change item. Keeping them apart from `validations` keeps two different questions
apart: "is the thing we propose to write valid" and "is the thing you already have valid".

FOUR STATUSES, ENFORCED BY A CHECK CONSTRAINT. `passed`, `failed`, `tool_missing` and `errored` are four
different facts, and a constraint is what stops a future writer collapsing the last two into one of the
first two — which is precisely how a security control comes to fabricate a verdict.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scan_artifact_validations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        # The tool may be empty when it was never reached — a path that could not be read has no tool.
        sa.Column("tool", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("tool_version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.String(length=1024), nullable=False, server_default=""),
        # Nullable, because an invented line number is worse than none: it sends a reader to the wrong
        # place confidently. `helm lint` reports about a chart, not a line.
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'tool_missing', 'errored')",
            name="ck_scan_artifact_validations_status",
        ),
        sa.UniqueConstraint("project_id", "path", "kind", name="uq_scan_artifact_validations_project_path_kind"),
    )
    op.create_index(
        "ix_scan_artifact_validations_project",
        "scan_artifact_validations",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_scan_artifact_validations_project", table_name="scan_artifact_validations")
    op.drop_table("scan_artifact_validations")
