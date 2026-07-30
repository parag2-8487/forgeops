# SPDX-License-Identifier: FSL-1.1-ALv2
"""audit_events, append-only and enforced by the database.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-30

Design: §6.3, §6.4, §6.5, §11.9, Appendix E criterion 9, Q-05.

Three mechanisms, none of which is sufficient alone:

1. `seq BIGSERIAL` gives a total order per database, so a deletion leaves a gap.
2. `hash = sha256(canonical(payload) || prev_hash)` chains the rows, so editing an
   old row invalidates every later hash — detectable with no second copy.
3. UPDATE, DELETE and TRUNCATE raise `42501` in a trigger **and** are revoked from
   `forgeops_app`. The trigger stops an ORM bug or a stray statement run as any
   role; the REVOKE stops the trigger being dropped by the application, because
   dropping a trigger needs ownership the app role does not have.

`0002`'s `ALTER DEFAULT PRIVILEGES` already granted the app role full DML on every
table created after it, so this revision expresses immutability as a *narrowing* —
a REVOKE of three verbs — rather than as a grant it might forget to write. That
ordering is deliberate: the default is permissive, the exception is explicit and
local, and `scripts/check-db-roles.py` asserts the exception actually took.

`project_id` and `actor_user_id` carry no foreign key. An immutable log that
cascades away when a project is deleted is not an immutable log.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "forgeops_app"

IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (design §6.3, Q-05): % attempted', TG_OP
        USING ERRCODE = '42501';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_device_id", sa.Uuid(), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_kind", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.String(length=1024), nullable=False),
        sa.Column("before_state", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("after_state", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column("prev_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("seq", name="pk_audit_events"),
        sa.UniqueConstraint("id", name="uq_audit_events_id"),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_project_id", "audit_events", ["project_id"])
    op.create_index("ix_audit_project_created", "audit_events", ["project_id", "created_at"])
    op.create_index("ix_audit_actor_created", "audit_events", ["actor_user_id", "created_at"])
    op.create_index("ix_audit_resource", "audit_events", ["resource_kind", "resource_id"])

    # --- mechanism 3a: the trigger ------------------------------------------
    op.execute(IMMUTABLE_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_audit_events_no_update BEFORE UPDATE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()"
    )
    op.execute(
        "CREATE TRIGGER trg_audit_events_no_delete BEFORE DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()"
    )
    # TRUNCATE fires a statement-level trigger; FOR EACH ROW is not permitted.
    op.execute(
        "CREATE TRIGGER trg_audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
        "EXECUTE FUNCTION audit_events_immutable()"
    )

    # --- mechanism 3b: the privilege ----------------------------------------
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM {APP_ROLE}")
    op.execute(f"GRANT INSERT, SELECT ON audit_events TO {APP_ROLE}")
    # The sequence behind `seq BIGSERIAL`. Postgres names it `<table>_<column>_seq`.
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE audit_events_seq_seq TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_truncate ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_delete ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_update ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_immutable()")
    op.drop_index("ix_audit_resource", table_name="audit_events")
    op.drop_index("ix_audit_actor_created", table_name="audit_events")
    op.drop_index("ix_audit_project_created", table_name="audit_events")
    op.drop_index("ix_audit_events_project_id", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_id", table_name="audit_events")
    op.drop_table("audit_events")
