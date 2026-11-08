"""Notifications and per-user channel preferences. §2.6.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-22

Two tables. `notifications` is the record of what was raised and what became of each delivery attempt —
stored rather than fire-and-forget, because "was anybody told?" is a question an incident review asks and a
webhook POST that vanished cannot answer it. `notification_preferences` is per user per channel, with a
CHECK keeping the channel inside the closed set.

`delivery` is a jsonb map from channel to outcome, so a notification delivered to Slack and rejected by SMTP
records both — a single status column would have to choose one and would hide the other.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = ("deploy_completed", "deploy_failed", "policy_violated", "approval_required", "self_healed")
_CHANNELS = ("in_app", "slack", "discord", "email")


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # What it is about, so the bell can link to it. Nullable: a policy violation may name no deployment.
        sa.Column("resource_kind", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("delivery", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in _KINDS) + ")",
            name="ck_notifications_kind",
        ),
    )
    op.create_index("ix_notifications_project_created", "notifications", ["project_id", "created_at"])

    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # The webhook or address this user's channel delivers to. NULL for `in_app`, which needs none.
        sa.Column("target", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "channel IN (" + ", ".join(f"'{channel}'" for channel in _CHANNELS) + ")",
            name="ck_notification_preferences_channel",
        ),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in _KINDS) + ")",
            name="ck_notification_preferences_kind",
        ),
        # A CHANNEL THAT DELIVERS SOMEWHERE MUST SAY WHERE. An enabled Slack preference with no webhook is
        # a setting that silently does nothing, which is worse than a refusal at the point of saving it.
        sa.CheckConstraint(
            "channel = 'in_app' OR NOT enabled OR target IS NOT NULL",
            name="ck_notification_preferences_target_present",
        ),
        sa.UniqueConstraint("user_id", "project_id", "channel", "kind", name="uq_notification_preference"),
    )


def downgrade() -> None:
    op.drop_table("notification_preferences")
    op.drop_index("ix_notifications_project_created", table_name="notifications")
    op.drop_table("notifications")
