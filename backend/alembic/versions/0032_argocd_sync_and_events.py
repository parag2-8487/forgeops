"""`argocd.app_action` joins the vocabulary, and the repository-event table.

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-22

§2.7. Two changes that belong together because they are the same feature.

THE TABLE IS WHAT MAKES THE WEBHOOK SAFE. The obvious webhook implementation — receive a repository change,
sync the Application — would be an unauthenticated HTTP request causing a production deployment, which is a
hole straight through everything §3 exists to do. So the webhook RECORDS into this table and raises a
notification; a human then syncs through the governed route. The auto-sync §2.7 asks for is real and lives
in the Application manifest's `automated:` block, where enabling it is a decision about where authority lies
for that application rather than a consequence of exposing an endpoint.

NO FOREIGN KEY TO `projects`. A repository event arrives before anything has matched it to a project — the
payload names a repository URL, and more than one project may track it. Attributing it at write time would
mean guessing, and a wrong guess puts an entry in the wrong history.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATIONS = (
    "changeset.apply",
    "repository.clone",
    "deployment.apply_manifests",
    "docker.container_action",
    "docker.image_action",
    "kubernetes.workload_action",
    "devtools.run",
    "iac.apply",
    "argocd.app_action",
)


def upgrade() -> None:
    op.drop_constraint("ck_change_sets_operation", "change_sets", type_="check")
    op.create_check_constraint(
        "ck_change_sets_operation",
        "change_sets",
        "operation IN (" + ", ".join(f"'{name}'" for name in _OPERATIONS) + ")",
    )

    op.create_table(
        "argocd_repository_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("repository", sa.String(length=1024), nullable=False, index=True),
        # The revision may be empty: a ping event from a forge names a repository and no commit, and
        # recording it is still useful — it proves the webhook is wired, which is otherwise unanswerable.
        sa.Column("revision", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Ordered lookups per repository: "what has changed recently" is the only query this table serves.
    op.create_index(
        "ix_argocd_repository_events_repository_received_at",
        "argocd_repository_events",
        ["repository", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_argocd_repository_events_repository_received_at", "argocd_repository_events")
    op.drop_table("argocd_repository_events")
    # The operation vocabulary is deliberately left wide. See `0025`'s downgrade for the full reasoning:
    # narrowing it fails against any database holding a row in the removed state, and the alternative is
    # deleting governance records to satisfy a constraint.
