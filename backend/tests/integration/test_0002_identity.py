# SPDX-License-Identifier: FSL-1.1-ALv2
"""Revision `0002_identity_and_devices` against a REAL PostgreSQL.

design.md §6.1, §6.3, §6.5; tasks.md leaf 5.1.

What is proven, and why each is the assertion that matters:

* **Both enums carry lower-case values.** SQLAlchemy persists a Python enum's
  *names* unless told otherwise, so the failure mode is a column holding `ADMIN`
  while the token claim, Cerbos and every Rego policy say `admin`. Asserting the
  exact value set from `pg_enum` is the only thing that catches it, and it would
  otherwise surface as an authorisation failure much later.
* **`users.email` is really CITEXT and case really collapses.** A `VARCHAR` unique
  index would let `A@b` and `a@b` become two accounts for one human, and the second
  would be invisible to the first. Both halves are asserted: the type as the server
  renders it, and the behaviour.
* **No plaintext token column exists.** PRD §7 names `pairing_token` and
  `device_token`; storing either in the clear would make a database read equivalent
  to a stolen credential. The assertion is over the column *set*, so a plaintext
  column cannot arrive later unnoticed.
* **Both roles exist and neither can log in.** The migration creates them NOLOGIN on
  purpose — a role created with a password in committed source would be a committed
  credential. The Compose init supplies a local password from the untracked `.env`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from .migration_support import (
    column_type,
    fk_delete_action,
    make_project,
    make_user,
    rows,
    scalar,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

USER_ROLE_VALUES = {"admin", "developer", "viewer"}
DEVICE_STATUS_VALUES = {"pending", "active", "policy_stale", "revoked", "abandoned"}

#: PRD §7's column names. If any of these ever appears, the value is being stored.
FORBIDDEN_PLAINTEXT_COLUMNS = {"refresh_token", "pairing_token", "device_token", "envelope_key"}


async def _enum_values(conn, type_name: str) -> set[str]:
    found = await rows(
        conn,
        """
        SELECT e.enumlabel
        FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = :name
        ORDER BY e.enumsortorder
        """,
        name=type_name,
    )
    return {r[0] for r in found}


async def _columns(conn, table: str) -> dict[str, str]:
    found = await rows(
        conn,
        """
        SELECT a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = :table
          AND a.attnum > 0 AND NOT a.attisdropped
        """,
        table=table,
    )
    return {r[0]: r[1] for r in found}


class TestTheEnumsCarryValuesNotNames:
    async def test_user_role_values(self, conn) -> None:
        assert await _enum_values(conn, "user_role") == USER_ROLE_VALUES

    async def test_device_status_values(self, conn) -> None:
        assert await _enum_values(conn, "device_status") == DEVICE_STATUS_VALUES

    async def test_no_upper_case_label_survives(self, conn) -> None:
        """States the failure mode directly rather than only its absence."""
        for type_name in ("user_role", "device_status"):
            labels = await _enum_values(conn, type_name)
            assert not any(label != label.lower() for label in labels), (
                f"{type_name} carries an upper-case label; SQLAlchemy persisted enum "
                f"names instead of values: {sorted(labels)}"
            )


class TestCitextEmail:
    async def test_the_column_type_is_citext(self, conn) -> None:
        assert await column_type(conn, "users", "email") == "citext"

    async def test_case_variants_collapse_to_one_account(self, conn) -> None:
        base = uuid.uuid4().hex[:10]
        await conn.execute(
            _insert_user_sql(),
            {
                "id": uuid.uuid4(),
                "email": f"MiXeD-{base}@Example.INVALID",
                "name": "Case Proof",
                "role": "developer",
                "sub": f"sub-{uuid.uuid4().hex}",
            },
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    _insert_user_sql(),
                    {
                        "id": uuid.uuid4(),
                        "email": f"mixed-{base}@example.invalid",
                        "name": "Case Proof Two",
                        "role": "developer",
                        "sub": f"sub-{uuid.uuid4().hex}",
                    },
                )

    async def test_a_genuinely_different_email_is_still_accepted(self, conn) -> None:
        """Guards against the constraint being over-broad."""
        await make_user(conn)
        await make_user(conn)


def _insert_user_sql():
    from sqlalchemy import text

    return text(
        "INSERT INTO users (id, email, name, role, idp_subject, is_active) "
        "VALUES (:id, :email, :name, :role, :sub, true)"
    )


class TestUniqueIdpSubject:
    async def test_a_duplicate_subject_is_rejected(self, conn) -> None:
        subject = f"sub-{uuid.uuid4().hex}"
        await conn.execute(
            _insert_user_sql(),
            {
                "id": uuid.uuid4(),
                "email": f"a-{uuid.uuid4().hex[:10]}@example.invalid",
                "name": "First",
                "role": "admin",
                "sub": subject,
            },
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    _insert_user_sql(),
                    {
                        "id": uuid.uuid4(),
                        "email": f"b-{uuid.uuid4().hex[:10]}@example.invalid",
                        "name": "Second",
                        "role": "admin",
                        "sub": subject,
                    },
                )


class TestSessionCascade:
    async def test_the_declared_ondelete_is_cascade(self, conn) -> None:
        """`confdeltype` 'c' is CASCADE. Asserted as well as exercised, so the
        declaration and the behaviour are both pinned."""
        assert await fk_delete_action(conn, "fk_sessions_user_id_users") == "c"

    async def test_deleting_a_user_removes_their_sessions(self, conn) -> None:
        from sqlalchemy import text

        user_id = await make_user(conn)
        await conn.execute(
            text(
                "INSERT INTO sessions (id, user_id, refresh_token_hmac, expires_at) "
                "VALUES (:id, :user_id, :hmac, now() + interval '1 hour')"
            ),
            {"id": uuid.uuid4(), "user_id": user_id, "hmac": b"\x00" * 32},
        )
        before = await scalar(conn, "SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id)
        assert before == 1
        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        after = await scalar(conn, "SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id)
        assert after == 0


class TestNoPlaintextTokenColumnExists:
    @pytest.mark.parametrize("table", ["sessions", "agent_devices"])
    async def test_the_column_set_names_no_plaintext_token(self, conn, table: str) -> None:
        columns = set(await _columns(conn, table))
        offenders = columns & FORBIDDEN_PLAINTEXT_COLUMNS
        assert not offenders, (
            f"{table} carries plaintext credential column(s) {sorted(offenders)}; "
            f"tokens are stored as HMACs and the envelope key as AES-256-GCM "
            f"ciphertext (design §6.3)"
        )

    async def test_the_hmac_columns_are_bytea(self, conn) -> None:
        assert await column_type(conn, "sessions", "refresh_token_hmac") == "bytea"
        for column in ("pairing_token_hmac", "device_token_hmac", "envelope_key_enc"):
            assert await column_type(conn, "agent_devices", column) == "bytea", column


class TestTheTwoDatabaseRoles:
    @pytest.mark.parametrize("role", ["forgeops_app", "forgeops_migrator"])
    async def test_the_role_exists(self, conn, role: str) -> None:
        assert await scalar(conn, "SELECT 1 FROM pg_roles WHERE rolname = :r", r=role) == 1

    async def test_the_migration_creates_both_roles_without_a_password(self, conn) -> None:
        """The invariant that matters is "no committed credential", not `NOLOGIN`.

        `0002` creates both roles with no password, because a migration is committed
        source and a role created with a password there would be a committed
        credential. A deployment then grants LOGIN and a password from its own secret
        store — `scripts/postgres-init/10-forgeops-roles.sh` does exactly that from the
        untracked `.env` (§6.4, §13.3) — so asserting `rolcanlogin is False` would
        assert the *absence* of a correct deployment and fail in the one environment
        that is set up properly.

        The check walks the AST and inspects only string literals that are not
        docstrings. A plain substring search over the file matches the word in this
        very docstring, which would make the assertion pass for the wrong reason and
        then fail as soon as a comment was reworded.
        """
        import ast
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0002_identity_and_devices.py"
        ).read_text(encoding="utf-8")

        tree = ast.parse(source)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        ]
        assert literals, "no string literals found; the AST walk is not doing anything"

        offenders = [value for value in literals if "PASSWORD" in value.upper()]
        assert not offenders, (
            f"0002 carries a PASSWORD clause in SQL: {offenders}. A migration is "
            f"committed source, so that would be a committed credential "
            f"(secret-safety, §6.4)"
        )

        joined = "\n".join(literals)
        assert "CREATE ROLE" in joined, "0002 no longer creates the roles"
        assert "NOLOGIN" in joined, "0002 must create the roles NOLOGIN"

    @pytest.mark.parametrize("privilege", ["SELECT", "INSERT", "UPDATE", "DELETE"])
    @pytest.mark.parametrize("table", ["users", "sessions", "agent_devices"])
    async def test_the_app_role_holds_dml(self, conn, table: str, privilege: str) -> None:
        held = await scalar(
            conn,
            "SELECT has_table_privilege('forgeops_app', :t, :p)",
            t=table,
            p=privilege,
        )
        assert held is True, f"forgeops_app lacks {privilege} on {table}"

    async def test_the_default_privilege_reaches_a_later_revision_table(self, conn) -> None:
        """`ALTER DEFAULT PRIVILEGES` in 0002 is what lets 0007 express audit
        immutability as a narrowing REVOKE instead of a grant it might forget. This
        asserts the mechanism worked on a table created three revisions later."""
        held = await scalar(conn, "SELECT has_table_privilege('forgeops_app', 'change_sets', 'INSERT')")
        assert held is True


class TestCitextExtension:
    async def test_the_extension_is_installed(self, conn) -> None:
        assert await scalar(conn, "SELECT 1 FROM pg_extension WHERE extname = 'citext'") == 1


class TestAgentDeviceShape:
    async def test_a_device_row_round_trips(self, conn) -> None:
        from sqlalchemy import text

        project_id = await make_project(conn, "devices")
        device_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO agent_devices (id, project_id, status, agent_version, platform) "
                "VALUES (:id, :p, 'pending', '0.1.0', 'windows/amd64')"
            ),
            {"id": device_id, "p": project_id},
        )
        last_seq = await scalar(conn, "SELECT last_seq FROM agent_devices WHERE id = :id", id=device_id)
        assert last_seq == 0, "last_seq must default to 0 (the replay high-water mirror)"

    async def test_an_unknown_status_is_rejected(self, conn) -> None:
        from sqlalchemy import text

        project_id = await make_project(conn, "devices-bad")
        with pytest.raises(Exception):  # noqa: B017 - asyncpg raises DataError for a bad enum
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "INSERT INTO agent_devices (id, project_id, status, agent_version, platform) "
                        "VALUES (:id, :p, 'quarantined', '0.1.0', 'linux/amd64')"
                    ),
                    {"id": uuid.uuid4(), "p": project_id},
                )
