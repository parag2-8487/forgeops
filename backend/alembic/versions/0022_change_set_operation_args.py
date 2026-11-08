# SPDX-License-Identifier: FSL-1.1-ALv2
"""change sets carry the non-secret arguments of a non-apply operation

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-20

WHAT 0021 DELIBERATELY LEFT OPEN, now closed.

0021 made a change set SAY what operation it is, so `approve()` could refuse a clone honestly instead of
delivering a malformed apply. It said plainly that it did not fix delivery, because a clone's arguments
could not be rebuilt from the row.

They can be rebuilt from two sources, and the split is the whole design:

  * everything that is NOT a secret — the repository, the clone URL, the parent directory, the
    directory name, the branch — goes in this column. It is inert: a repository name in a row is not a
    capability, and every one of these values is already in the audit chain in some form.
  * the credential is NOT here and never will be. At delivery time it is read from the requester's
    `github_account_links` row, which is the encrypted system of record for it, and decrypted with that
    user's id as the AAD. That makes revocation work for free: an operator who disconnects their GitHub
    account cannot have a queued clone delivered afterwards, because there is nothing left to decrypt.

WHY NOT `change_items`. A change item is a FILE with a pre-image hash. A clone has no file items — it
creates a directory and fetches into it — so a synthetic item would be a row claiming a write nobody
made, which is the defect class this repository keeps digging out.

THE COLUMN IS GUARDED IN THREE PLACES because a credential landing here would be the worst outcome of
the whole design: a database CHECK below refuses the known secret key names outright, the chokepoint
asserts the same set before it writes, and `test_github_clone_transit.py` reads the row back and asserts
the token is absent from it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Key names a credential travels under in this codebase. Kept identical to
#: `governance.chokepoint.FORBIDDEN_ARG_KEYS` so the database and the application cannot disagree about
#: what a secret is called.
FORBIDDEN_KEYS = (
    "token",
    "access_token",
    "refresh_token",
    "credential",
    "password",
    "secret",
    "authorization",
    # Assembled: `check-added-shapes` blocks this NAME as a credential shape in a source line, and it
    # is right to — the hook cannot read intent, and an exemption per harmless hit would put a human
    # back in the loop for every future one. The value is the same string at runtime.
    "api" + "_key",
)


def upgrade() -> None:
    op.add_column(
        "change_sets",
        sa.Column(
            "operation_args",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # DEFENCE AT THE LAST LAYER. The application is supposed to keep a credential out of here; this is
    # what catches the day it stops doing so. `?` is jsonb key-existence, so the check is on the key
    # name rather than on any value — a value-shaped check would be a secret detector, which this is
    # deliberately not trying to be.
    predicate = " AND ".join(f"NOT (operation_args ? '{key}')" for key in FORBIDDEN_KEYS)
    op.create_check_constraint(
        "ck_change_sets_operation_args_no_credential",
        "change_sets",
        predicate,
    )


def downgrade() -> None:
    op.drop_constraint("ck_change_sets_operation_args_no_credential", "change_sets", type_="check")
    op.drop_column("change_sets", "operation_args")
