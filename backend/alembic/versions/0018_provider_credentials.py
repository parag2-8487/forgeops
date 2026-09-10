# SPDX-License-Identifier: FSL-1.1-ALv2
"""provider credentials and operator-defined endpoints

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-07

WHY THESE ARE NOT ROWS IN `secrets`.

`secrets` is scoped by `project_id` and `environment` — it holds the values a project's own deployment
needs. A provider API key is neither: one OpenAI key serves every project in the deployment, and scoping it
to a project would mean re-entering it per project and would make "which key did this run use" depend on
which project asked.

`key_ref` IS THE PRIMARY KEY, and that is what makes this table a drop-in for the environment. The tier
config already names a `key_ref` per endpoint (`openai`, `xai`, `anthropic`), and `EnvKeyResolver` maps that
to `LLM_KEY_<REF>`. This table answers the same question from the same key, so the resolver can consult it
first and fall back to the environment — an operator who has configured keys in `.env` keeps working, and
one who sets a key in the UI overrides it without editing a file.

THE VALUE IS SEALED WITH THE SAME PRIMITIVE AS `secrets.encrypted_value`: AES-256-GCM under
`LOCAL_SECRET_SEAL_KEY`, nonce prefixed. Not a second scheme — a second scheme is a second thing to get
wrong, and §7.11 already forbids plaintext at rest.

`provider_endpoints` is separate because an operator-defined endpoint is CONFIGURATION, not a secret: a base
URL and a model name are not sensitive, and keeping them out of the sealed table means they can be listed
and edited without decrypting anything.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        # The tier config's own `key_ref`, so this table answers the same question the environment does.
        sa.Column("key_ref", sa.String(length=64), primary_key=True),
        # Nonce-prefixed AES-256-GCM ciphertext, exactly as `secrets.encrypted_value`.
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        # WHAT THE VALUE LOOKS LIKE, NEVER THE VALUE. A length and a last-four let the UI show that
        # something is configured, and let an operator tell two keys apart, without a read path that
        # returns the secret. §7.11 keeps values out of anything that leaves the process.
        sa.Column("value_length", sa.Integer(), nullable=False),
        sa.Column("value_hint", sa.String(length=8), nullable=False, server_default=""),
        # The outcome of the last real call made with this key, so the UI reports evidence rather than
        # presence. NULL means never tested — which is not the same as failing, and must not render as it.
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_detail", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("value_length > 0", name="ck_provider_credentials_value_nonempty"),
    )

    op.create_table(
        "provider_endpoints",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        # Only `openai_compatible` is accepted at the API, because it is the only protocol with an adapter.
        # Stored anyway so a future protocol does not need a migration to be recorded.
        sa.Column(
            "protocol",
            sa.String(length=32),
            nullable=False,
            server_default="openai_compatible",
        ),
        # Points at `provider_credentials.key_ref`. Nullable because a local server needs no key, which is
        # the case the self-hosted tier already relies on.
        sa.Column("key_ref", sa.String(length=64), nullable=True),
        sa.Column("tier", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint(
            "base_url LIKE 'http://%' OR base_url LIKE 'https://%'",
            name="ck_provider_endpoints_base_url_scheme",
        ),
    )


def downgrade() -> None:
    op.drop_table("provider_endpoints")
    op.drop_table("provider_credentials")
