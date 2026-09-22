"""Self-healing actions, and the post-incident record.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-23

Phase 2 §2.12. One table for what was done and one for what was concluded afterwards.

THE CONSTRAINTS HERE ARE GUARD RAILS, not validation. `ck_healing_actions_auto_is_safe` is the one that
matters: it refuses any row claiming `auto = true` for a remedy outside the safe set. That means the
two-tier split holds even if a future code path gets the decision wrong -- the database is the only place
that cannot be bypassed by a new caller, and self-healing is precisely the feature where a new caller with a
plausible-looking condition is the likely failure.

The remedy vocabulary is duplicated between here and `healing.py`'s frozensets, deliberately and with a test
asserting they agree. The alternative -- a single Python source of truth -- leaves the database willing to
store anything, and a guard rail that only exists in the language that has the bug is not a guard rail.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | None = None
depends_on: str | None = None

# Kept as literals so the CHECK text is readable in the migration. `test_healing.py` asserts these match
# `healing.SAFE_REMEDIES` and `RISKY_REMEDIES` exactly, so the two cannot drift.
_SAFE = ("restart_container", "restart_pod")
_RISKY = ("rollback_deployment", "scale_workload", "apply_configuration_change")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    op.create_table(
        "healing_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("remedy", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        # FALSE by default: a row written by a path that forgot to set it reads as "a human was involved",
        # which understates the system's autonomy rather than overstating it.
        sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("change_set_id", sa.Uuid(), sa.ForeignKey("change_sets.id"), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in_list("remedy", _SAFE + _RISKY), name="ck_healing_actions_remedy"),
        sa.CheckConstraint(
            "state IN ('proposed', 'executing', 'succeeded', 'failed', 'refused')",
            name="ck_healing_actions_state",
        ),
        # THE GUARD RAIL. No row may claim auto-execution for a remedy outside the safe set, whatever code
        # wrote it. This is the two-tier split expressed where it cannot be bypassed: a future caller with a
        # plausible `if severity == 'critical'` cannot auto-execute a rollback, because the insert fails.
        sa.CheckConstraint(
            f"auto = false OR {_in_list('remedy', _SAFE)}",
            name="ck_healing_actions_auto_is_safe",
        ),
        # A risky remedy that was executed must carry the change set that approved it. Without this, an
        # approval-required action could be recorded as having run with nothing to point at.
        sa.CheckConstraint(
            "auto = true OR state NOT IN ('executing', 'succeeded') OR change_set_id IS NOT NULL",
            name="ck_healing_actions_risky_needs_change_set",
        ),
    )
    op.create_index("ix_healing_actions_incident", "healing_actions", ["incident_id", "created_at"])
    op.create_index("ix_healing_actions_created_at", "healing_actions", ["created_at"])

    op.create_table(
        "incident_postmortems",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
        # `generated` vs `unavailable` vs `insufficient`: the same honesty the RCA table needs, for the same
        # reason. A summary row that defaulted to "generated" with empty text would read as "nothing to say".
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        # Long-term recommendations, separate from the summary because they have a different audience and a
        # different lifetime: a summary is read once during review, a recommendation is acted on later.
        sa.Column("recommendations", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("model", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("actions_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ('generated', 'unavailable', 'insufficient')",
            name="ck_incident_postmortems_state",
        ),
        # Same shape as the RCA constraint: a generated summary must have content, and a non-generated one
        # must not -- so "not generated" can never be mistaken for "generated and found nothing".
        sa.CheckConstraint(
            "state <> 'generated' OR length(summary) > 0",
            name="ck_incident_postmortems_generated_has_content",
        ),
        sa.CheckConstraint(
            "state = 'generated' OR length(summary) = 0",
            name="ck_incident_postmortems_ungenerated_is_empty",
        ),
    )
    op.create_index("ix_incident_postmortems_incident", "incident_postmortems", ["incident_id", "created_at"])


def downgrade() -> None:
    op.drop_table("incident_postmortems")
    op.drop_table("healing_actions")
