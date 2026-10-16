"""Record which command a change set was delivered as.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-22

WHY. §2.2's live-log box needs to subscribe to the agent's progress for one deployment, and progress is
published on a per-command Redis channel. The command id was a fresh UUID that nothing kept, so a
deployment could not find its own stream. Nullable because a change set that was blocked or is awaiting
approval has no command yet — and that is the honest representation: `null` means "not delivered", not
"delivered as nothing".

It also closes a smaller gap: the audit chain could say a change set was delivered and not which command
carried it, so correlating an agent-side log with a governance record meant matching timestamps.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("change_sets", sa.Column("command_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("change_sets", "command_id")
