# SPDX-License-Identifier: FSL-1.1-ALv2
"""Operator-supplied provider credentials and endpoints.

WHY THESE EXIST. The tier configuration names a `key_ref` per endpoint and `EnvKeyResolver` maps it to
`LLM_KEY_<REF>` in the environment. That works for a deployment configured from a file, and it leaves an
operator with no way to add a key without editing `.env` and restarting the process — so a fresh install
reported six model tiers, three of them "available", with nothing but shipped placeholders behind them.

These tables answer the SAME question from the SAME key, so the resolver can consult them first and fall
back to the environment. Nobody's existing configuration changes.

DECLARED HERE AND NOT ONLY IN THE MIGRATION. `alembic check` compares `SQLModel.metadata` against the
migrated schema and proposes dropping anything the models do not know about, so a table created by a
migration alone would be deleted by the next autogenerate.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    func,
)
from sqlmodel import Field, SQLModel

#: The only protocol with an adapter, and therefore the only one this API accepts.
SUPPORTED_CUSTOM_PROTOCOL = "openai_compatible"


class ProviderCredential(SQLModel, table=True):
    """One provider API key, sealed, keyed by the `key_ref` the tier config already uses.

    NOT A ROW IN `secrets`, which is scoped by project and environment. A provider key is neither: one
    OpenAI key serves every project in the deployment, and scoping it per project would mean re-entering it
    for each one and would make "which key did this run use" depend on which project asked.

    THE VALUE NEVER LEAVES THE PROCESS. There is no read path that returns it — `value_length` and
    `value_hint` exist so the UI can show that something is configured, and let an operator tell two keys
    apart, without ever handing the secret back. §7.11 keeps values out of anything that leaves the process,
    and a "reveal" button is exactly the affordance that ends up in a screenshot.
    """

    __tablename__ = "provider_credentials"
    __table_args__ = (CheckConstraint("value_length > 0", name="ck_provider_credentials_value_nonempty"),)

    key_ref: str = Field(primary_key=True, max_length=64)
    #: Nonce-prefixed AES-256-GCM ciphertext, the same primitive as `secrets.encrypted_value`.
    encrypted_value: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    value_length: int = Field(sa_column=Column(Integer, nullable=False))
    #: The last few characters, so two keys for one provider are distinguishable. Never the whole value.
    value_hint: str = Field(default="", max_length=8)

    #: The outcome of the last REAL call made with this key.
    #:
    #: NULL means never tested, which is not the same as failing and must not render as it. A key's presence
    #: is not evidence that it works — that was the original defect on the Models screen, one layer up.
    last_tested_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    last_test_ok: bool | None = Field(default=None, sa_column=Column(Boolean, nullable=True))
    last_test_detail: str = Field(default="", max_length=1024)

    updated_by: uuid.UUID | None = Field(default=None, foreign_key="users.id", ondelete="SET NULL")
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    updated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))


class ProviderEndpoint(SQLModel, table=True):
    """An endpoint an operator defined, rather than one the shipped tier file declares.

    Separate from `ProviderCredential` because this is CONFIGURATION, not a secret: a base URL and a model
    name are not sensitive, so keeping them out of the sealed table means they can be listed and edited
    without decrypting anything.

    The base URL's scheme is constrained in the database as well as validated at the API, because a value
    that reaches this column by any other route still has to be a URL something can dial.
    """

    __tablename__ = "provider_endpoints"
    __table_args__ = (
        CheckConstraint(
            "base_url LIKE 'http://%' OR base_url LIKE 'https://%'",
            name="ck_provider_endpoints_base_url_scheme",
        ),
    )

    id: str = Field(primary_key=True, max_length=64)
    model: str = Field(max_length=200)
    base_url: str = Field(max_length=500)
    protocol: str = Field(
        default=SUPPORTED_CUSTOM_PROTOCOL,
        max_length=32,
        sa_column_kwargs={"server_default": SUPPORTED_CUSTOM_PROTOCOL},
    )
    #: Points at `provider_credentials.key_ref`. Nullable because a local server needs no key — the case the
    #: self-hosted tier already relies on.
    key_ref: str | None = Field(default=None, max_length=64)
    tier: str = Field(max_length=32)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    updated_by: uuid.UUID | None = Field(default=None, foreign_key="users.id", ondelete="SET NULL")
