"""`iac.apply` joins the change-set operation vocabulary.

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-22

§2.2's OpenTofu apply. It creates and destroys real infrastructure and bills for it, so it travels the same
six stages as every other mutation and the CHECK has to admit it.

Phase 1 deliberately had no apply at all, and `iac.Runner`'s contract test said so in as many words. The
absence was not squeamishness about the verb: there was nothing to hold the state lock, no approval to
attach an apply to, and no saved plan to apply — so an apply then would have re-planned at run time and
applied whatever it found, which is not what any human approved. All three now exist.

The downgrade leaves the vocabulary wide, for the reason `0024`, `0025` and `0027` record: narrowing it
fails against any database where one of these has run, and the alternative is deleting governance records
to satisfy a constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
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
