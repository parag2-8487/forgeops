# SPDX-License-Identifier: FSL-1.1-ALv2
"""environments, their variables and their approval requirement

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-20

PHASE 2 STARTS HERE BECAUSE EVERYTHING ELSE IN IT READS THIS.

A deployment goes TO an environment; a promotion goes BETWEEN two; a rollback restores what an
environment held; progressive delivery shifts traffic WITHIN one; the Command Center's "deploy to
staging" names one. Building any of those first would mean inventing a placeholder for the thing they
all reference, and a placeholder on a runtime path is the defect class this repository keeps digging out.

THE APPROVAL REQUIREMENT IS THE POINT OF THE TABLE, not an attribute of it.

`GovernanceChokepoint._evaluate_policy` has taken an `environment` argument since Phase 1 and nothing
ever supplied one — a parameter with the right name that no caller filled, which is why a clone's policy
evaluation fell through to "requires approval because `environment` is absent". `requires_approval` here
is what finally gives that argument a value derived from operator intent: production requires a human,
a scratch environment need not, and the difference is a row rather than a constant in the policy bundle.

ORDERING IS A COLUMN (`position`) BECAUSE PROMOTION IS ORDERED. "Promote to the next environment" is
meaningless without a sequence, and deriving one from the name would make `staging` and `stage` different
pipelines. A unique constraint on (project, position) keeps the sequence total rather than merely
suggested.

SECRETS ARE SEALED WITH THE SAME CONSTRUCTION THE GITHUB LINK USES — AES-256-GCM under a key derived
from `ENVELOPE_PEPPER`, with the environment id as the AAD, so a sealed value lifted into another
environment's row fails to open rather than decrypting into the wrong context. `is_secret` decides
whether a value is sealed on the way in and withheld on the way out; a plain variable is readable
because an operator has to be able to see what they configured.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The four §2.1 names, plus `custom` so an operator is not forced into a vocabulary that does not
#: describe their pipeline. A closed set even so: a free-text kind would make "is this production?" a
#: string comparison at every call site that has to decide how careful to be.
ENVIRONMENT_KINDS = ("development", "test", "staging", "production", "custom")


def upgrade() -> None:
    op.create_table(
        "environments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        # The kubectl context the agent must select before touching a cluster for this environment.
        # Nullable: an environment can exist before anyone has wired a cluster to it, and pretending
        # otherwise would force an operator to invent a context name to save a row.
        sa.Column("k8s_context", sa.String(length=253), nullable=True),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # DEFAULTS TO REQUIRING APPROVAL. The safe direction is the default direction: a new environment
    # nobody has thought about yet behaves like production, and an operator opts OUT deliberately.
    op.create_check_constraint(
        "ck_environments_kind",
        "environments",
        "kind IN (" + ", ".join(f"'{kind}'" for kind in ENVIRONMENT_KINDS) + ")",
    )
    # A name is how a human and the Command Center both refer to an environment, so it has to be
    # unambiguous within its project.
    op.create_unique_constraint("uq_environments_project_name", "environments", ["project_id", "name"])
    # And the sequence has to be total: two environments at the same position make "the next one"
    # a coin toss.
    op.create_unique_constraint("uq_environments_project_position", "environments", ["project_id", "position"])
    op.create_index("ix_environments_project", "environments", ["project_id"])

    op.create_table(
        "environment_variables",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "environment_id",
            sa.Uuid(),
            sa.ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=128), nullable=False),
        # Exactly one of these holds the value, enforced below. Two columns rather than one, because a
        # single column would make "is this ciphertext?" a guess about the bytes.
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value_sealed", sa.LargeBinary(), nullable=True),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint("uq_environment_variables_env_key", "environment_variables", ["environment_id", "key"])
    # THE INVARIANT THAT KEEPS A SECRET FROM BEING STORED IN CLEAR. A row claiming `is_secret` with a
    # populated `value` would read as protected and be readable; the database refuses it rather than
    # trusting every future writer to get the branch right.
    op.create_check_constraint(
        "ck_environment_variables_exactly_one_value",
        "environment_variables",
        "(is_secret AND value IS NULL AND value_sealed IS NOT NULL) OR "
        "(NOT is_secret AND value IS NOT NULL AND value_sealed IS NULL)",
    )


def downgrade() -> None:
    op.drop_table("environment_variables")
    op.drop_index("ix_environments_project", table_name="environments")
    op.drop_table("environments")
