# SPDX-License-Identifier: FSL-1.1-ALv2
"""Reconcile `change_sets.status` with design §3.6's state machine.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-31

Design: §3.6, §6.5, §17.1 D-63, Appendix A.3, Appendix B Q-22.

Why a tenth revision when §6.5's plan stops at `0009`
-----------------------------------------------------
`0004` generated `ck_change_sets_status_allowed` from `CHANGE_SET_STATUSES`, which is
exactly the arrangement §6.5 asks for — but the tuple itself had been written from
memory rather than from §3.6. It carried `validated`, `awaiting_approval` and `failed`,
which §3.6 does not define, and was missing `rejected_by_policy`, `blocked`,
`pending_approval`, `expired`, `conflicted` and `reverted`, which it does.

Three of the six outcomes Appendix A.3's transit produces are therefore unstorable on a
database at `0009`: a blast-radius block writes `blocked`, the approval gate writes
`pending_approval`, and a completed revert writes `reverted`. Leaf 7.5 cannot be
implemented against that constraint, and Q-22 — "only edges in the §3.6 state machine
are accepted" — cannot be asserted against a schema that rejects three of its states.

D-63 records the decision and what it rejected. The short form: the alternative was to
map A.3's outcomes onto the nearest surviving names (`blocked → rejected`,
`pending_approval → awaiting_approval`, `reverted → rolled_back`), which would make the
audit trail say a human rejected a change set the blast-radius analyser blocked. A
schema that cannot express the design is a smaller problem than a schema that
misrepresents it.

Why this is safe to run forwards
--------------------------------
`change_sets` is empty in every environment this revision can reach: it was created by
`0004` and the only writer is `GovernanceChokepoint`, which lands in the same commit as
this revision. The check below asserts that rather than assuming it, and fails loudly if
a row carries a status the new vocabulary would reject — which is the one case where a
silent constraint swap would corrupt a lifecycle.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.governance.models import CHANGE_SET_STATUSES, in_list

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_change_sets_status_allowed"

#: The vocabulary `0004` created, written out as a literal **because it no longer exists
#: in code**. A downgrade has to restore the historical constraint, and the only honest
#: place for a superseded list is the revision that superseded it.
PREVIOUS_STATUSES: tuple[str, ...] = (
    "draft",
    "validating",
    "validated",
    "awaiting_approval",
    "approved",
    "rejected",
    "applying",
    "applied",
    "failed",
    "rolled_back",
)


def _statuses_outside(allowed: tuple[str, ...]) -> list[str]:
    """The distinct `change_sets.status` values that `allowed` would not permit."""
    connection = op.get_bind()
    rendered = ", ".join(f"'{value}'" for value in allowed)
    result = connection.execute(sa.text(f"SELECT DISTINCT status FROM change_sets WHERE status NOT IN ({rendered})"))
    return sorted(str(value) for value in result.scalars())


def upgrade() -> None:
    """Widen the constraint, refusing if any stored status falls outside §3.6's vocabulary.

    Postgres validates a new CHECK against existing rows and would raise anyway, but its message
    names the constraint rather than the offending values. Raising here names the values, because
    "which statuses are in the table" is the only question an operator hitting this needs answered.

    Widening is validated rather than grandfathered: three of `0004`'s ten names (`validated`,
    `awaiting_approval`, `failed`) are not in §3.6 and this revision drops them, so a row carrying
    one would become unreadable by the state machine. `change_sets` has no writer before this
    commit — `GovernanceChokepoint` lands with it — so the assertion should hold trivially.
    Asserting it is how that stops being an assumption.
    """
    stranded = _statuses_outside(CHANGE_SET_STATUSES)
    if stranded:
        raise RuntimeError(
            f"cannot widen {CONSTRAINT}: change_sets holds status values {stranded} that design "
            "§3.6's vocabulary does not define. Migrate those rows to a §3.6 state first; a "
            "constraint swap must not silently strand a lifecycle."
        )
    op.drop_constraint(CONSTRAINT, "change_sets", type_="check")
    # Generated from the tuple, exactly as `0004` does, so the application and the
    # database still cannot drift: a state added to §3.6's list without a revision
    # leaves `alembic check` unhappy rather than leaving the database permissive.
    op.create_check_constraint(CONSTRAINT, "change_sets", in_list("status", CHANGE_SET_STATUSES))


def downgrade() -> None:
    """Restore `0004`'s narrower vocabulary as `NOT VALID`, grandfathering existing rows.

    The asymmetry with `upgrade` is deliberate. A downgrade that *refused* whenever a row carried
    `blocked`, `pending_approval` or `reverted` would be a downgrade nobody can run — and the first
    thing it breaks is `alembic downgrade base`, which every §6.5 revision proof runs before it
    migrates up. That is not hypothetical: it was observed the first time this revision was written
    with a symmetric guard.

    `NOT VALID` says the right thing instead. The narrower vocabulary applies to every INSERT and
    UPDATE from here on, and rows written while the wider vocabulary was in force stay readable
    rather than being deleted. A downgrade must never destroy a lifecycle to satisfy a constraint,
    and the residue is printed rather than hidden.
    """
    grandfathered = _statuses_outside(PREVIOUS_STATUSES)
    if grandfathered:
        print(  # noqa: T201 - a migration's only channel to the operator running it
            f"alembic 0010 downgrade: {CONSTRAINT} restored as NOT VALID because existing rows "
            f"carry {grandfathered}, which revision 0004's vocabulary does not permit. Those rows "
            "are left intact and readable; new writes are constrained."
        )
    op.drop_constraint(CONSTRAINT, "change_sets", type_="check")
    op.execute(
        f"ALTER TABLE change_sets ADD CONSTRAINT {CONSTRAINT} "
        f"CHECK ({in_list('status', PREVIOUS_STATUSES)}) NOT VALID"
    )
