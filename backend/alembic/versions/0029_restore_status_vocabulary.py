"""Restore §3.6's change-set status vocabulary.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-22

WHY THIS EXISTS AT ALL — a wrong turn, recorded rather than quietly rebased away.

`record_command_result` has always written `status = 'failed'` when an agent reports a command failed, and
`failed` has not been storable since revision `0010` REMOVED it, precisely because design §3.6 does not
define that state. So the failure branch raised `CheckViolationError` inside the handler: the path could
never have worked, and nothing had exercised it until a settler test drove it end to end.

The first fix widened the vocabulary to admit `failed`. That was the wrong direction — it changed the
authority to accommodate a bug — and `test_0010_change_set_statuses.py` said so immediately: it asserts
`failed` is REJECTED by the constraint, because `0010`'s whole point was that `0004`'s tuple had been
written from memory rather than from §3.6.

So the code was fixed toward the authority instead: a failed result now records `rolled_back`, §3.6's only
failure edge out of `applying`. This revision puts the constraint back to exactly what `0010` installed. It
is a revision rather than an edit to `0028` because a database that already ran the widened version needs a
forward step to return, and rewriting history would leave those databases unreachable.

No finer outcome is lost: for a deployment, `deployments.status` carries `degraded` versus `failed`
separately, which is where per-operation detail belongs.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Exactly `0010`'s list, which is exactly §3.6's states. Kept identical to
#: `governance.models.CHANGE_SET_STATUSES`.
_STATUSES = (
    "draft",
    "validating",
    "rejected_by_policy",
    "blocked",
    "pending_approval",
    "approved",
    "rejected",
    "expired",
    "applying",
    "applied",
    "rolled_back",
    "conflicted",
    "reverted",
)


def _check() -> str:
    return "status IN (" + ", ".join(f"'{status}'" for status in _STATUSES) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_change_sets_status_allowed", "change_sets", type_="check")
    op.create_check_constraint("ck_change_sets_status_allowed", "change_sets", _check())


def downgrade() -> None:
    # Nothing to undo: this revision's purpose is to hold the constraint at §3.6's vocabulary, and the
    # state it returns to is the one `0010` already installed.
    pass
