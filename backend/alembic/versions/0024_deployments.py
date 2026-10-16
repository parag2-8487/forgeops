# SPDX-License-Identifier: FSL-1.1-ALv2
"""deployments, with the stable-state snapshot §2.2 asks for

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-21

WHAT A DEPLOYMENT ROW IS FOR, beyond a record that something happened.

§2.2 asks for a deployment record and a "stable-state snapshot per successful deploy", and §2.3 asks for
history, a diff between any two deployments, and a rollback to any previous one. All three read THIS
table, so the columns are chosen for what those questions need rather than for what an apply happens to
produce:

  * `manifest_digest` and `manifests` make a DIFF possible. A row that recorded only "deployed at 14:02"
    could not answer "what changed between these two", which is §2.3's second box.
  * `healthy` is separate from `status`, because "the cluster accepted the objects" and "the workloads
    came up" are different facts. The agent reports both and collapsing them would make a deployment that
    never came up indistinguishable from one that did — and 2.12's self-healing branches on exactly that.
  * `stable` marks the rows a rollback may target. A deployment that was applied and never became healthy
    is NOT a stable state to return to, so the snapshot is recorded on health rather than on apply. This
    is the whole reason the two columns are separate.
  * `environment_id` is a real foreign key, so a deployment cannot name an environment that does not
    exist and the approval requirement that governed it is always recoverable.

`change_set_id` LINKS BACK TO THE GOVERNANCE RECORD. A deployment is a mutation and therefore has a
change set, an approval and an audit chain; the deployment row is a projection for the timeline, not a
second source of truth about whether it was allowed. Nullable only because the column would otherwise
have to be filled before the transit that creates the change set has run.

WHAT IS DELIBERATELY NOT HERE. No logs: a deployment's log stream is large, append-only and read once,
which is a different storage problem from a record read by a timeline. No image digest column yet — image
build and push is a separate §2.2 box and is not built, so a column for its output would be a column
nothing writes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The lifecycle. `degraded` is the one worth explaining: the manifests are in the cluster and at least
#: one workload did not converge. It is not a failure — nothing needs retrying, the objects are there —
#: and it is not a success, because the thing the operator asked for is not running.
DEPLOYMENT_STATUSES = (
    "pending_approval",
    "applying",
    "applied",
    "degraded",
    "failed",
    "rolled_back",
)


def upgrade() -> None:
    # THE CHANGE-SET OPERATION VOCABULARY GROWS WITH THIS TABLE, and in the same revision on purpose.
    # `0021` installed a CHECK over two operations so `approve()` could not guess; a deployment is the
    # third, and adding the table without widening the constraint would make every deployment transit
    # fail on an INSERT — which is the correct failure, and a confusing one to debug.
    op.drop_constraint("ck_change_sets_operation", "change_sets", type_="check")
    op.create_check_constraint(
        "ck_change_sets_operation",
        "change_sets",
        "operation IN ('changeset.apply', 'repository.clone', 'deployment.apply_manifests')",
    )

    op.create_table(
        "deployments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "environment_id",
            sa.Uuid(),
            sa.ForeignKey("environments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        # RESTRICT rather than CASCADE on the environment: deleting an environment must not erase the
        # history of what was deployed to it. §2.3's timeline is the reason — a rollback target that
        # vanished when somebody tidied up an environment list is a rollback that cannot be audited.
        sa.Column(
            "change_set_id",
            sa.Uuid(),
            sa.ForeignKey("change_sets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        # Two separate facts. See the module docstring.
        sa.Column("healthy", sa.Boolean(), nullable=True),
        sa.Column("stable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("manifests", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest_digest", sa.String(length=71), nullable=False),
        sa.Column("cluster_context", sa.String(length=253), nullable=True),
        sa.Column("namespace", sa.String(length=253), nullable=True),
        # The agent's own report, stored whole. Its words rather than a parse of them: an operator
        # debugging a rollout wants "1 of 3 updated replicas are available", and a summary loses it.
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_deployments_status",
        "deployments",
        "status IN (" + ", ".join(f"'{status}'" for status in DEPLOYMENT_STATUSES) + ")",
    )
    # A STABLE ROW MUST BE HEALTHY. The invariant that makes `stable` usable as a rollback target: a
    # deployment that was applied and never converged is not a state to return to. Enforced here rather
    # than only in the service, because the service is one writer and a support UPDATE is another.
    op.create_check_constraint(
        "ck_deployments_stable_implies_healthy",
        "deployments",
        "NOT stable OR (healthy AND status = 'applied')",
    )
    op.create_index("ix_deployments_project_created", "deployments", ["project_id", "created_at"])
    op.create_index("ix_deployments_environment", "deployments", ["environment_id"])
    # The rollback target lookup: the newest stable deployment of an environment. A partial index
    # because that is the only query that reads `stable`, and it is read on every rollback.
    op.create_index(
        "ix_deployments_stable_targets",
        "deployments",
        ["environment_id", "created_at"],
        postgresql_where=sa.text("stable"),
    )


def downgrade() -> None:
    op.drop_index("ix_deployments_stable_targets", table_name="deployments")
    op.drop_index("ix_deployments_environment", table_name="deployments")
    op.drop_index("ix_deployments_project_created", table_name="deployments")
    op.drop_table("deployments")
    # THE OPERATION CONSTRAINT IS DELIBERATELY LEFT WIDE, and this is not laziness.
    #
    # The first version of this downgrade narrowed it back to two operations, and the test harness's
    # `alembic downgrade base` failed on it: `check constraint "ck_change_sets_operation" is violated by
    # some row`. Every deployment that had ever run left a `change_sets` row saying
    # `deployment.apply_manifests`, and the narrowed constraint cannot represent it.
    #
    # The two ways out are to delete those rows or to leave the vocabulary wide. Deleting them is not
    # available: a change set is a governance record with an approval and an audit chain hanging off it,
    # and a migration that erases them to satisfy a CHECK would destroy exactly the evidence §1.9 exists
    # to keep. Leaving the constraint wide costs nothing — the column still cannot hold an arbitrary
    # string, and `0021`'s purpose (stop `approve()` guessing) is unaffected — so that is what happens,
    # stated here rather than discovered by the next person to downgrade.
