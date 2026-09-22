"""`devtools.run` joins the change-set operation vocabulary.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-22

§2.8's dev-tools proxy. Running the project's own tests, linters, build, compose stack or migrations is a
change set because it executes the repository's scripts on the operator's machine — the largest execution
authority in the catalogue — so it travels the same six stages as an apply and the CHECK has to admit it.

The downgrade leaves the vocabulary wide, for the reason `0024` and `0025` record: narrowing it fails
against any database where one of these has run, and the alternative is deleting governance records to
satisfy a constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
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
)


def upgrade() -> None:
    op.drop_constraint("ck_change_sets_operation", "change_sets", type_="check")
    op.create_check_constraint(
        "ck_change_sets_operation",
        "change_sets",
        "operation IN (" + ", ".join(f"'{name}'" for name in _OPERATIONS) + ")",
    )


def downgrade() -> None:
    # Deliberately left wide. See `0025`'s downgrade for the full reasoning.
    pass
