# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generation run provenance, phase timings, and repair of synthetically-stamped project tenants.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-02

WHY THE GENERATION COLUMNS EXIST
--------------------------------
A run could be pinned to one provider (Gemini or the local Ollama chain) from the Manual Generation page,
and the row has to say which was ASKED FOR as well as which ANSWERED (`endpoint_id`). Without
`requested_endpoint_id` an audit cannot tell "Gemini was selected and failed" from "nothing was selected and
the default chain ran", and those mean different things about whether a provider was silently swapped.

`timings` is per-phase wall-clock evidence (context preparation, compilation, model, first token, validation,
total). Investigations into "generation took ten minutes" had nothing in the row to say where the minutes
went; the only record was a live SSE stream that is gone once the request ends.

`failure_reason` is why a run ended `failed`. Runs abandoned mid-stream (browser closed, backend restarted)
used to stay `running` forever with no attempts recorded, which is indistinguishable from a run that is
still working.

WHY THE TENANT REPAIR EXISTS, AND WHY IT IS NARROW
------------------------------------------------
Images built before commit 70a47fc stamped `projects.tenant_id = uuid5(NAMESPACE_URL,
'urn:forgeops:project-tenant:<project id>')` onto TENANTLESS projects at the start of every autonomous
deployment. Every list and read query scopes by the caller's tenant, and the operator's user has none, so
each project vanished from the UI the moment a deployment started -- while its row, its file tree and its
runs were all still in the database.

The repair resets ONLY rows whose tenant equals that exact derived value, which is a fact about how the
value got there rather than a guess: no human-assigned tenant can collide with a UUIDv5 of the project's own
id. Projects in genuine tenants are untouched, and nothing is deleted.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | None = None
depends_on: str | None = None

_SYNTHETIC_TENANT_URN = "urn:forgeops:project-tenant:{}"
#: Every table carrying both `project_id` and `tenant_id`, read from information_schema on 2026-11-02.
_PROJECT_SCOPED_TENANT_TABLES = (
    "agent_devices",
    "analysis_reports",
    "change_sets",
    "deployments",
    "environments",
    "generation_runs",
    "notifications",
    "policies",
    "policy_bundles",
    "secrets",
)


def synthetic_project_tenant(project_id: uuid.UUID) -> uuid.UUID:
    """The tenant the pre-70a47fc autonomous deployer derived for a tenantless project."""
    return uuid.uuid5(uuid.NAMESPACE_URL, _SYNTHETIC_TENANT_URN.format(project_id))


def repair_synthetic_project_tenants(bind: sa.engine.Connection) -> list[uuid.UUID]:
    """Reset every project tenant equal to the derived synthetic value. Returns the repaired project ids.

    A module function so the integration suite can prove the repair on real rows without replaying the
    whole migration chain.
    """
    repaired: list[uuid.UUID] = []
    rows = bind.execute(sa.text("SELECT id, tenant_id FROM projects WHERE tenant_id IS NOT NULL")).all()
    for project_id, tenant_id in rows:
        if tenant_id == synthetic_project_tenant(project_id):
            # Child rows that inherited the synthetic tenant are reset with the project, so a project that
            # becomes visible again does not have runs and reports that its owner still cannot read.
            for table in _PROJECT_SCOPED_TENANT_TABLES:
                bind.execute(
                    sa.text(f"UPDATE {table} SET tenant_id = NULL WHERE project_id = :p AND tenant_id = :t"),
                    {"p": project_id, "t": tenant_id},
                )
            bind.execute(
                sa.text("UPDATE projects SET tenant_id = NULL WHERE id = :p AND tenant_id = :t"),
                {"p": project_id, "t": tenant_id},
            )
            repaired.append(project_id)
    return repaired


def upgrade() -> None:
    op.add_column("generation_runs", sa.Column("requested_endpoint_id", sa.String(100), nullable=True))
    op.add_column("generation_runs", sa.Column("requested_model", sa.String(200), nullable=True))
    op.add_column(
        "generation_runs",
        sa.Column(
            "timings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("generation_runs", sa.Column("failure_reason", sa.Text(), nullable=True))

    repair_synthetic_project_tenants(op.get_bind())


def downgrade() -> None:
    # The tenant repair is not reversed: re-hiding projects is not a state anyone should be able to restore.
    op.drop_column("generation_runs", "failure_reason")
    op.drop_column("generation_runs", "timings")
    op.drop_column("generation_runs", "requested_model")
    op.drop_column("generation_runs", "requested_endpoint_id")
