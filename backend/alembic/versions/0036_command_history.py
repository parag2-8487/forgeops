"""Command Center history.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-23

Phase 2 2.5. One table, and `outcome` is the column that matters: it distinguishes an utterance that was
INTERPRETED from one that was DISPATCHED from one that was REFUSED. Without that a user whose sentence was
declined cannot tell a refusal from a broken button, and "nothing happened" is the least debuggable report
there is.

There is deliberately NO column holding an operation the client supplied, because the client never supplies
one -- `detail` records the operation the SERVER resolved, which is the only one that ever existed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "command_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # The raw text, kept so a user can see what was understood from what they typed. Empty on an
        # execute, which names an intent rather than a sentence.
        sa.Column("utterance", sa.Text(), nullable=False, server_default=""),
        # Empty when classification refused: there was no intent, and recording a guess would misrepresent
        # what happened.
        sa.Column("intent", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("session_key", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "outcome IN ('interpreted', 'dispatched', 'read', 'refused')",
            name="ck_command_history_outcome",
        ),
        # A refusal must carry its reason, or the history says "declined" and nothing more.
        sa.CheckConstraint(
            "outcome <> 'refused' OR length(detail) > 0",
            name="ck_command_history_refusal_has_reason",
        ),
    )
    op.create_index("ix_command_history_session", "command_history", ["session_key", "created_at"])
    op.create_index("ix_command_history_created_at", "command_history", ["created_at"])


def downgrade() -> None:
    op.drop_table("command_history")
