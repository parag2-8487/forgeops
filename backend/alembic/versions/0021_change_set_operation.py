# SPDX-License-Identifier: FSL-1.1-ALv2
"""change sets state their operation

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-19

WHY THIS COLUMN EXISTS, and the defect that produced it.

`repository.clone` became the third mutating operation, and a clone is submitted as a change set so it
travels the same six stages as an apply. `chokepoint.approve()` then delivered it — with
`operation=changeset.apply` and `args` built by `_apply_entries`, because those were the only shapes it
knew. The agent correctly refused ("a change set with no entries") and the set reached `rolled_back`,
which reads as an agent problem and is actually the backend having sent the wrong command. Found by
running a real clone against a real agent; every unit and integration test passed, because the
auto-approved path mints its own envelope and never goes through `approve()`.

So a change set now SAYS what it is, and `approve()` reads it instead of assuming. A default of
`changeset.apply` is correct for every existing row: those are the only kind that existed.

WHAT THIS COLUMN DOES NOT FIX, stated here so the next reader does not assume it does. A clone's
envelope carries a short-lived GitHub credential, and that credential is deliberately not stored
anywhere — so `approve()` cannot REBUILD a clone's arguments from the row. Knowing the operation lets it
REFUSE honestly, naming why, instead of delivering a malformed apply. Delivering a human-approved clone
needs the credential re-minted at delivery time from the link of the user who asked (`change_sets.
created_by`), which is recorded as Phase 2 backlog rather than half-built here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "change_sets",
        sa.Column(
            "operation",
            sa.String(length=64),
            nullable=False,
            server_default="changeset.apply",
        ),
    )
    # A closed set, enforced by the database rather than only by the application: `approve()` branches
    # on this value to decide whether it can deliver at all, and a typo would put it back in the
    # position this column exists to leave — guessing.
    op.create_check_constraint(
        "ck_change_sets_operation",
        "change_sets",
        "operation IN ('changeset.apply', 'repository.clone')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_change_sets_operation", "change_sets", type_="check")
    op.drop_column("change_sets", "operation")
