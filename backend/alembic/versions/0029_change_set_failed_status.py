"""`failed` joins the change-set status vocabulary.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-22

A PRE-EXISTING DEFECT, surfaced by the first test that drove the failure path end to end.
`record_command_result` has always written `status = 'failed'` when an agent reports a command failed — its
own comment explains why `failed` rather than `rolled_back` — and `failed` was never in
`ck_change_sets_status_allowed`. So any real failed command raised `CheckViolationError` inside the result
handler, which means the path could never have worked and nothing had exercised it.

The fix is the vocabulary, not the intent: the author's distinction is right. `rolled_back` means the agent
undid its own work and said so through `agent.error`; `failed` means a `command.result` arrived reporting
the operation did not succeed. Those are different facts and collapsing them would lose the one an operator
needs.

`failed` is TERMINAL, like `applied` and `rolled_back`: a result has arrived and the set is not going to
move again on its own.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
    "failed",
    "rolled_back",
    "conflicted",
    "reverted",
)


def upgrade() -> None:
    op.drop_constraint("ck_change_sets_status_allowed", "change_sets", type_="check")
    op.create_check_constraint(
        "ck_change_sets_status_allowed",
        "change_sets",
        "status IN (" + ", ".join(f"'{status}'" for status in _STATUSES) + ")",
    )


def downgrade() -> None:
    # Left wide, for the reason `0025`'s downgrade records: narrowing fails against any database holding a
    # row in the removed state, and the alternative is deleting governance records.
    pass
