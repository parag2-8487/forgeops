# SPDX-License-Identifier: FSL-1.1-ALv2
"""The per-user GitHub account link, as a model.

DECLARED HERE AND NOT ONLY IN THE MIGRATION. `alembic check` compares `SQLModel.metadata` against the
migrated schema and proposes DROPPING anything the models do not know about, so a table created by a
migration alone is a table the next autogenerate deletes. `test_alembic_autogenerate_clean.py` turns that
into a failure now rather than a surprise in a later diff — and it is what caught this table.

NOTHING READS THIS CLASS AT RUNTIME, and that is deliberate rather than an oversight. `service.py` uses
explicit SQL through `text()`, as the rest of this codebase does for the same reason: the read paths must
be able to select a column set that CANNOT name the sealed token columns, which an ORM row does by
default. So this file's job is to state the schema for the migration contract, and the docstring says so
because a class with no caller is otherwise exactly the defect class this repository keeps finding.

The two columns holding ciphertext are `LargeBinary`. There is no property or accessor for them here: a
model with a `token` attribute is a token in every `repr`, every log line that formats a row, and every
serialiser that walks attributes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, DateTime, LargeBinary, func
from sqlmodel import Field, SQLModel


class GitHubAccountLink(SQLModel, table=True):
    """One user's link to one GitHub account.

    `user_id` IS THE PRIMARY KEY, so a user has at most one link and re-connecting replaces it rather
    than accumulating rows. The foreign key cascades on delete: this credential is replayable, so an
    orphan row would be a live credential nobody owns.
    """

    __tablename__ = "github_account_links"
    __table_args__ = (
        CheckConstraint("length(github_login) > 0", name="ck_github_links_login_nonempty"),
        CheckConstraint("octet_length(access_token_sealed) > 12", name="ck_github_links_sealed_nonempty"),
        CheckConstraint(
            "credential_kind IN ('oauth_app', 'personal_token')",
            name="ck_github_links_credential_kind",
        ),
    )

    user_id: uuid.UUID = Field(primary_key=True, foreign_key="users.id", ondelete="CASCADE")
    #: Nullable like every other `tenant_id` in this schema: D-35 defers enforced tenancy and
    #: `test_tenant_context.py` asserts no table has jumped ahead of that decision. Nothing writes NULL.
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    github_login: str = Field(max_length=39)
    github_user_id: int = Field(sa_column=Column(BigInteger(), nullable=False))
    github_avatar_url: str = Field(default="", max_length=500)
    scopes: str = Field(default="", max_length=500)
    credential_kind: str = Field(default="oauth_app", max_length=32)
    access_token_sealed: bytes = Field(sa_column=Column(LargeBinary(), nullable=False))
    access_token_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    refresh_token_sealed: bytes | None = Field(default=None, sa_column=Column(LargeBinary(), nullable=True))
    refresh_token_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    #: Tri-state evidence of the last real call: never used, failed, or worked. `last_use_ok` is
    #: nullable because "never used" is not "fine" and must not render as it.
    last_used_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    last_use_ok: bool | None = Field(default=None, sa_column=Column(Boolean(), nullable=True))
    last_use_detail: str = Field(default="", max_length=1024)
