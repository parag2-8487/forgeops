# SPDX-License-Identifier: FSL-1.1-ALv2
"""github account links

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-19

WHY THIS IS NOT A ROW IN `provider_credentials` OR IN `secrets`.

`provider_credentials` is keyed by `key_ref` and is deployment-wide: one OpenAI key serves every project
and every user. `secrets` is keyed by `(project_id, environment)`: it holds what a project's deployment
needs. A GitHub account link is neither — it belongs to ONE platform user, it is the answer to "what can
this person see on GitHub", and putting it in either table would make that answer shared. A shared row
here is not a tidiness problem: it would let one user's repositories be listed under another user's
session, which is the isolation this table's primary key exists to make impossible.

`user_id` IS THE PRIMARY KEY, so a user has at most one link and re-connecting replaces it rather than
accumulating rows. `tenant_id` is stored as well as the user's own, denormalised deliberately: every read
predicates on both, so a link cannot be read across a tenant boundary even if a user id were guessed.

THE TOKEN COLUMNS ARE SEALED, not hashed — unlike a device token, this credential has to be REPLAYED to
GitHub, so a one-way hash is not an option. AES-256-GCM under an HKDF-derived key, nonce-prefixed, AAD
bound to the user id, exactly as `auth/devices.py` seals an envelope key and for the same reasons. The
HKDF label is new (`forgeops-github-link-v1`), so this scheme is domain-separated from both the pepper's
HMAC use and the envelope-key KEK.

WHAT IS STORED IN PLAINTEXT IS WHAT A SCREEN NEEDS AND NOTHING MORE: the GitHub login, the account id,
the scopes granted and when. None of it is a credential, and all of it is needed to say "connected as
<login>" without decrypting anything. There is deliberately no column for the token's value length or a
last-four hint: unlike a provider API key, nobody needs to tell two GitHub tokens apart by eye.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_account_links",
        # ONE LINK PER USER. `ondelete="CASCADE"`, because a deleted user's GitHub credential must not
        # outlive them — it is replayable, so an orphan row is a live credential nobody owns.
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Denormalised on purpose: every read predicates on user AND tenant (§6.7 makes tenancy a
        # row-level property), so this column is what makes the cross-tenant test assertable.
        #
        # NULLABLE, like every other `tenant_id` in this schema, and that is a repository-wide invariant
        # rather than a local choice: D-35 defers enforced tenancy, and
        # `test_tenant_context.py::test_tenant_id_columns_remain_nullable` reads
        # `information_schema` to assert no table has jumped ahead of that decision. A `NOT NULL` here
        # would make this one table refuse a row every other table accepts.
        #
        # Nothing writes NULL: the value comes from `principal.tenant_id`, and a read predicate of
        # `tenant_id = :tenant_id` does not match NULL — so a row without a tenant would be invisible
        # rather than visible to everyone, which is the safe direction for the failure that cannot
        # happen yet.
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        # What the screen shows. Not a credential.
        sa.Column("github_login", sa.String(length=39), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_avatar_url", sa.String(length=500), nullable=False, server_default=""),
        # Space-separated, as GitHub returns them. Recorded so the UI can say what the link may do
        # rather than implying it may do everything.
        sa.Column("scopes", sa.String(length=500), nullable=False, server_default=""),
        # Nonce-prefixed AES-256-GCM ciphertext. Never returned by a route, never logged, never in an
        # audit row.
        sa.Column("access_token_sealed", sa.LargeBinary(), nullable=False),
        # GitHub App user-to-server tokens expire in eight hours and carry a refresh token when the
        # App has expiring tokens enabled. Nullable because an App configured WITHOUT expiry issues
        # neither an expiry nor a refresh token, and inventing one would make a live token look dead.
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token_sealed", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        # The outcome of the last real call made with this link, so the UI reports evidence rather
        # than presence. NULL means never used, which is not the same as failing and must not render
        # as it — the same tri-state discipline the pairing screen's heartbeat uses.
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_use_ok", sa.Boolean(), nullable=True),
        sa.Column("last_use_detail", sa.String(length=1024), nullable=False, server_default=""),
        sa.CheckConstraint("length(github_login) > 0", name="ck_github_links_login_nonempty"),
        sa.CheckConstraint("octet_length(access_token_sealed) > 12", name="ck_github_links_sealed_nonempty"),
    )
    # The listing path reads by (user, tenant); the primary key covers the user half and this covers
    # the tenant half for an administrative "who has linked an account" question.
    op.create_index("ix_github_account_links_tenant_id", "github_account_links", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_github_account_links_tenant_id", table_name="github_account_links")
    op.drop_table("github_account_links")
