"""The deployment pipeline's circuit breaker.

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-22

§2.2. One row per project and environment, holding whether repeated validation failures have stopped
further deployments.

WHY A TABLE AND NOT MEMORY. Two backend replicas with in-memory breakers disagree about whether one is
open, and a deployment that is refused against one replica and accepted against the other is worse than no
breaker at all — it makes the refusal look random.

The UNIQUE constraint on (project_id, environment) is load-bearing: `record_validation_failure` is an
`ON CONFLICT ... DO UPDATE`, and without the constraint there is no conflict target, so two concurrent
failures would insert two rows and the count would be wrong in the direction that matters — too low, so the
breaker never opens.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = ("closed", "open", "half_open")


def upgrade() -> None:
    op.create_table(
        "deployment_circuit_breakers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # The environment NAME rather than a foreign key to `environments`. A breaker is a fact about
        # repeated failure against a target, and it must survive an environment row being recreated —
        # deleting and re-adding "production" should not silently clear a breaker that is protecting it.
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="closed"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_failure_reason", sa.Text(), nullable=True),
        # NULL when closed. An `opened_at` that kept its old value while closed would make the cooldown
        # calculation read from a past opening.
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "environment", name="uq_deployment_breakers_project_environment"),
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in _STATES) + ")",
            name="ck_deployment_breakers_state_allowed",
        ),
        # An OPEN breaker MUST have an `opened_at`, because the cooldown is computed from it: an open row
        # with a NULL opening would never become half-open and would stop deployments for ever.
        sa.CheckConstraint(
            "(state = 'open' AND opened_at IS NOT NULL) OR (state <> 'open')",
            name="ck_deployment_breakers_open_has_opened_at",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_deployment_breakers_failures_non_negative",
        ),
    )


def downgrade() -> None:
    op.drop_table("deployment_circuit_breakers")
