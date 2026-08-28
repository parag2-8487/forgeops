"""Give a stored policy the parameter values its rules read.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-29

WHAT WAS MISSING, AND WHY FR-32 COULD NOT WORK WITHOUT IT
---------------------------------------------------------
`policies` stored a name, a `template_id`, and `rego_rules`. It did not store the VALUES the rules
read. So a user could create a policy from the scheduling template, and there was nowhere to record
*which* weekday it blocks; the file-restrictions template had nowhere to record *which* globs it
protects.

The consequence was visible one layer up: `chokepoint.py` sent `"policy_parameters": {}` on every
stage-1 evaluation, with a comment stating the effect plainly — "no blocked weekday blocks nothing, no
glob protects nothing". `policies/agent/schedule.rego` and `paths.rego` are correct and were reading
`input.project.blocked_weekdays` and `input.project.protected_globs` out of an empty object, so they
could never fire. Meanwhile the criterion "Policies are enforced (block Friday deploys, require
approvals)" was ticked and the UI offered full policy CRUD, so a user could write a policy, see it
listed as enabled, and have it constrain nothing.

JSONB rather than five columns, because the parameter set is per template and templates are data:
`PROJECT_PARAMETER_KEYS` is the closed list of what reaches the bundle, and adding a template's
parameter should not need a migration. `server_default='{}'` so every existing row is immediately
valid — an enabled policy with no parameters is a policy that constrains nothing, which is exactly
what those rows meant before this column existed.

NOT NULL because a null here and an empty object would mean the same thing to every reader, and two
spellings of one state is how a query starts needing `COALESCE`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "policies",
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # Partial, and on `project_id` only where the policy is enabled. Stage 1 loads exactly this set —
    # the enabled policies of one project — on every mutation, so it is the hottest read the table
    # has. A full index would also cover the disabled rows, which that query never wants.
    op.create_index(
        "ix_policies_enabled_by_project",
        "policies",
        ["project_id"],
        unique=False,
        postgresql_where=sa.text("enabled"),
    )


def downgrade() -> None:
    op.drop_index("ix_policies_enabled_by_project", table_name="policies")
    op.drop_column("policies", "parameters")
