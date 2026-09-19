# SPDX-License-Identifier: FSL-1.1-ALv2
"""github link credential kind

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-19

WHY A COLUMN AND NOT AN INFERENCE. There are now two ways to link a GitHub account, and they are not
interchangeable in the two places it matters:

* a token pasted into ForgeOps CANNOT be revoked from here — GitHub has no API for an owner to revoke
  their own personal token — so a disconnect has to tell the user to delete it on GitHub, where a
  disconnect of an App authorization revokes it for them;
* a pasted token is whatever the person made it, so the screen must not imply the App's permissions.

Both of those are decisions a read path has to make, and inferring the kind from "is there a refresh
token" would be a guess that happens to be right today: a GitHub App with expiring tokens turned off
also issues no refresh token, so the inference would call an App link a pasted one.

`server_default='oauth_app'` because every row written before this migration came from the
authorization-code flow, which is the only path that existed. Backfilling with a guess is exactly what
the column exists to avoid, and here there is nothing to guess.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "github_account_links",
        sa.Column(
            "credential_kind",
            sa.String(length=32),
            nullable=False,
            server_default="oauth_app",
        ),
    )
    # A closed set, enforced by the database rather than only by the application: this column decides
    # whether a disconnect claims to have revoked anything, and a typo'd value would make that claim
    # fall through to the wrong branch.
    op.create_check_constraint(
        "ck_github_links_credential_kind",
        "github_account_links",
        "credential_kind IN ('oauth_app', 'personal_token')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_github_links_credential_kind", "github_account_links", type_="check")
    op.drop_column("github_account_links", "credential_kind")
