# SPDX-License-Identifier: FSL-1.1-ALv2
"""Identity and devices: citext, the two enums, users, sessions, agent_devices,
plus the two database roles the audit boundary depends on.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30

Design: §6.2, §6.3, §6.4, §6.5, §6.7.

Three things in here are not obvious and are deliberate.

**`citext` before the column.** `users.email` is CITEXT so that `A@b.com` and
`a@b.com` cannot become two accounts. The extension must exist before the column
that uses it, so it is created first, exactly as `0001` created `vector`.

**Enum values, not enum names.** Both enums are created with lower-case values
because that is what §6.2's ERD, the token claim and the Rego policies all say.
SQLAlchemy would otherwise persist the Python member *names*, and the only symptom
would be an authorisation failure much later.

**No password anywhere.** `forgeops_app` and `forgeops_migrator` are created
`NOLOGIN` with no password. A migration is committed source, so a role created with
a password here would be a committed credential. The Compose Postgres init grants
LOGIN and a local password from the untracked `.env` (§6.4, §13.3); a real
deployment does the same from its own secret store. `ALTER DEFAULT PRIVILEGES`
carries the app role's DML onto every table `0003` … `0009` will create, which is
what lets `0007` express audit immutability as a *narrowing* REVOKE rather than
having to remember a GRANT in each later revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "forgeops_app"
MIGRATOR_ROLE = "forgeops_migrator"

USER_ROLE_VALUES = ("admin", "developer", "viewer")
DEVICE_STATUS_VALUES = ("pending", "active", "policy_stale", "revoked", "abandoned")

# Tables created by 0001, before ALTER DEFAULT PRIVILEGES existed. They need an
# explicit grant; everything from 0003 onward is covered by the default.
PHASE_0_TABLES = ("projects", "file_tree", "embeddings")


def _create_role(name: str) -> None:
    """Idempotent role creation. Postgres has no CREATE ROLE IF NOT EXISTS, and a
    role is cluster-scoped, so a second database in the same cluster would fail a
    bare CREATE."""
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN
                    CREATE ROLE {name} NOLOGIN;
                END IF;
            END
            $$;
            """
        )
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    user_role = postgresql.ENUM(*USER_ROLE_VALUES, name="user_role", create_type=False)
    device_status = postgresql.ENUM(*DEVICE_STATUS_VALUES, name="device_status", create_type=False)
    user_role.create(op.get_bind(), checkfirst=True)
    device_status.create(op.get_bind(), checkfirst=True)

    # --- roles (§6.4, §6.7) -------------------------------------------------
    _create_role(APP_ROLE)
    _create_role(MIGRATOR_ROLE)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {MIGRATOR_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}")
    for table in PHASE_0_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")

    # --- users --------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("idp_subject", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("idp_subject", name="uq_users_idp_subject"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    # --- sessions -----------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("refresh_token_hmac", sa.LargeBinary(length=32), nullable=False),
        sa.Column("idp_session_id", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    # --- agent_devices ------------------------------------------------------
    op.create_table(
        "agent_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("status", device_status, nullable=False),
        sa.Column("pairing_token_hmac", sa.LargeBinary(length=32), nullable=True),
        sa.Column("device_token_hmac", sa.LargeBinary(length=32), nullable=True),
        sa.Column("envelope_key_enc", sa.LargeBinary(), nullable=True),
        sa.Column("cert_serial", sa.String(length=64), nullable=True),
        sa.Column("cert_fingerprint", sa.String(length=95), nullable=True),
        sa.Column("agent_version", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("policy_bundle_digest", sa.String(length=71), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("pairing_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cert_not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_devices"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_agent_devices_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("cert_serial", name="uq_agent_devices_cert_serial"),
    )
    op.create_index("ix_agent_devices_project_id", "agent_devices", ["project_id"])
    op.create_index("ix_agent_devices_tenant_id", "agent_devices", ["tenant_id"])
    op.create_index("ix_agent_devices_project_status", "agent_devices", ["project_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_agent_devices_project_status", table_name="agent_devices")
    op.drop_index("ix_agent_devices_tenant_id", table_name="agent_devices")
    op.drop_index("ix_agent_devices_project_id", table_name="agent_devices")
    op.drop_table("agent_devices")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")

    postgresql.ENUM(name="device_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="user_role").drop(op.get_bind(), checkfirst=True)

    # Revoke before dropping: a role that still holds a grant cannot be dropped,
    # and the default privileges must be withdrawn in the same form they were given.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}")
    for table in PHASE_0_TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {MIGRATOR_ROLE}")
    # The roles are deliberately NOT dropped. They are cluster-scoped and may be in
    # use by another database in the same cluster, and dropping a role out from under
    # a live connection is a worse outcome than leaving an unprivileged empty role.
    #
    # `citext` is deliberately NOT dropped either. `CREATE EXTENSION` requires
    # superuser for it, so `scripts/postgres-init/10-forgeops-roles.sh` creates it and
    # this revision's `CREATE EXTENSION IF NOT EXISTS` is a compatibility no-op for a
    # superuser-run migration. A migration must not drop an extension it may not have
    # created: under the §6.4 two-role arrangement `forgeops_migrator` does not own it,
    # so the DROP would fail and take the whole downgrade with it. An extension is
    # database infrastructure that outlives one schema revision.
