"""The dashboards' mutating actions join the change-set vocabulary.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-21

WHY A MIGRATION FOR THREE STRINGS. `0021` installed a CHECK over the change-set operation column so
`approve()` could not guess what a row meant, and every operation added since has had to widen it in the
same revision that made the operation reachable. `docker.container_action`, `docker.image_action` and
`kubernetes.workload_action` are the §2.4 and §2.9 dashboards' mutating half: a restart, a scale, a
rollback, an image pull and a container removal all change what is RUNNING on the operator's machine, so
each is a change set and travels the same six stages as an apply. Without this widening the transit's
INSERT fails — the correct failure, and a confusing one to debug from the panel that triggered it.

WHAT IS NOT HERE, deliberately: `docker.inventory` and `kubernetes.inventory`. They mutate nothing and are
what a refreshing panel calls. A change-set row per refresh would bury the audit records that matter under
thousands that do not, and `audit_events` is the log an operator reads after an incident.

No table is created, no column is added, and no row is rewritten: the vocabulary is the whole change.
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

#: Kept identical to `governance.models.CHANGE_SET_OPERATIONS`, which declares the same list so
#: `alembic check` holds the two together.
_OPERATIONS = (
    "changeset.apply",
    "repository.clone",
    "deployment.apply_manifests",
    "docker.container_action",
    "docker.image_action",
    "kubernetes.workload_action",
)


def _operation_check() -> str:
    quoted = ", ".join(f"'{name}'" for name in _OPERATIONS)
    return f"operation IN ({quoted})"


def upgrade() -> None:
    op.drop_constraint("ck_change_sets_operation", "change_sets", type_="check")
    op.create_check_constraint("ck_change_sets_operation", "change_sets", _operation_check())


def downgrade() -> None:
    # LEFT WIDE, for the reason `0024`'s downgrade records at length and which applies unchanged here.
    #
    # Narrowing the vocabulary on the way down fails against any database where one of these actions has
    # ever run: `check constraint "ck_change_sets_operation" is violated by some row`. The two ways out
    # are to delete those rows or to leave the vocabulary wide, and deleting them is not available — a
    # change set is a governance record with an approval and an audit chain hanging off it, and a
    # migration that erased them to satisfy a CHECK would destroy the evidence §1.9 exists to keep.
    #
    # The cost of leaving it wide is nothing that matters: the column still cannot hold an arbitrary
    # string, and `0021`'s purpose — stopping `approve()` from guessing — is unaffected by a vocabulary
    # that is larger than the code currently emits. Stated here rather than discovered by the next
    # person to run `alembic downgrade base`.
    pass
