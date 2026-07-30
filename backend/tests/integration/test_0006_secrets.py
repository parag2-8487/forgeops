# SPDX-License-Identifier: FSL-1.1-ALv2
"""Revision `0006_secrets` against a REAL PostgreSQL.

design.md §6.3, §6.5, §6.6, §17.1 D-50; tasks.md leaf 5.5.

Three assertions carry D-50, and one of them is an assertion of *absence*: there is
no `environment_id` column and no foreign key on `environment`. PRD D5 writes
`environment_id`, but `environments` is a Phase 2 table, and a nullable foreign key
to a table that does not exist is a broken reference rather than a seam. Testing that
the wrong thing was not built is unusual, and it is the point: without it, a later
"tidy-up" could add the dangling reference back and every other test would still pass.

The `SECRET_BACKEND=infisical` clause needs one honest sentence. The database does
**not** know which backend is configured — it cannot, because the setting lives in
the application's environment. What the schema enforces is the invariant that makes
the setting safe: exactly one of `infisical_path` and `encrypted_value` is non-null,
so a row can never be a second copy of a secret that Infisical already holds. Which
of the two is populated is decided above the schema, and `test_secrets_backend` in
the `secrets` package is where that belongs.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from src.secrets.models import SECRET_ENVIRONMENTS

from .migration_support import column_type, make_project, rows, scalar

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

INSERT_SECRET = text(
    "INSERT INTO secrets (id, project_id, environment, key, infisical_path, encrypted_value) "
    "VALUES (:id, :project_id, :environment, :key, :path, :value)"
)


async def _secret(
    conn,
    project_id: uuid.UUID,
    *,
    environment: str = "dev",
    key: str = "DATABASE_PASSWORD",
    path: str | None = "/forgeops/dev/DATABASE_PASSWORD",
    value: bytes | None = None,
) -> uuid.UUID:
    secret_id = uuid.uuid4()
    await conn.execute(
        INSERT_SECRET,
        {
            "id": secret_id,
            "project_id": project_id,
            "environment": environment,
            "key": key,
            "path": path,
            "value": value,
        },
    )
    return secret_id


class TestUniquenessPerProjectEnvironmentKey:
    async def test_a_duplicate_triple_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "secrets")
        await _secret(conn, project_id)
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _secret(conn, project_id)

    async def test_the_same_key_in_another_environment_is_allowed(self, conn) -> None:
        """The uniqueness is per environment on purpose: the same key holding a
        different value per environment is the normal case, not an error."""
        project_id = await make_project(conn, "secrets-envs")
        for environment in ("dev", "staging", "prod"):
            await _secret(
                conn,
                project_id,
                environment=environment,
                path=f"/forgeops/{environment}/DATABASE_PASSWORD",
            )
        count = await scalar(conn, "SELECT count(*) FROM secrets WHERE project_id = :p", p=project_id)
        assert count == 3

    async def test_the_same_key_in_another_project_is_allowed(self, conn) -> None:
        first = await make_project(conn, "secrets-p1")
        second = await make_project(conn, "secrets-p2")
        await _secret(conn, first)
        await _secret(conn, second)


class TestTheEnvironmentConstraint:
    @pytest.mark.parametrize("environment", SECRET_ENVIRONMENTS)
    async def test_each_of_the_four_names_is_accepted(self, conn, environment: str) -> None:
        """Parametrised over the model tuple. These are exactly the four names Phase 2's
        `environments` table will create, which is what makes its backfill a
        deterministic four-value map (D-50)."""
        project_id = await make_project(conn, f"secrets-{environment}")
        await _secret(conn, project_id, environment=environment)

    @pytest.mark.parametrize("environment", ["production", "PROD", "", "live"])
    async def test_anything_else_is_rejected(self, conn, environment: str) -> None:
        project_id = await make_project(conn, "secrets-bad-env")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _secret(conn, project_id, environment=environment)


class TestExactlyOneStorageLocation:
    async def test_infisical_path_alone_is_accepted(self, conn) -> None:
        project_id = await make_project(conn, "storage-infisical")
        await _secret(conn, project_id, path="/forgeops/dev/K", value=None)

    async def test_encrypted_value_alone_is_accepted(self, conn) -> None:
        """The `SECRET_BACKEND=local` development shape."""
        project_id = await make_project(conn, "storage-local")
        await _secret(conn, project_id, path=None, value=b"synthetic-sealed-bytes")

    async def test_both_set_is_rejected(self, conn) -> None:
        """Two copies of one secret is the state D-50 exists to forbid."""
        project_id = await make_project(conn, "storage-both")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _secret(conn, project_id, path="/forgeops/dev/K", value=b"synthetic-sealed-bytes")

    async def test_neither_set_is_rejected(self, conn) -> None:
        """A secret row that points nowhere is metadata pretending to be a secret."""
        project_id = await make_project(conn, "storage-neither")
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _secret(conn, project_id, path=None, value=None)


class TestTheDanglingReferenceWasNotBuilt:
    """Assertions of absence. D-50 resolved PRD D5's `environment_id` by *not*
    creating it; without a test, a later tidy-up could add it back and everything
    else would still pass."""

    async def test_there_is_no_environment_id_column(self, conn) -> None:
        assert await column_type(conn, "secrets", "environment_id") is None

    async def test_environment_is_constrained_text_not_a_reference(self, conn) -> None:
        assert await column_type(conn, "secrets", "environment") == "character varying(16)"

    async def test_the_only_foreign_key_is_to_projects(self, conn) -> None:
        found = await rows(
            conn,
            """
            SELECT conname, confrelid::regclass::text
            FROM pg_constraint
            WHERE conrelid = 'secrets'::regclass AND contype = 'f'
            """,
        )
        assert {(name, target) for name, target in found} == {("fk_secrets_project_id_projects", "projects")}, found

    async def test_no_environments_table_exists(self, conn) -> None:
        """Creating a stub would violate §1.3's no-stub rule and would put a Phase 2
        table under Phase 1's migration numbering."""
        present = await scalar(
            conn,
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = 'environments'",
        )
        assert present == 0


class TestNoPlaintextValueColumn:
    @pytest.mark.parametrize("column", ["value", "plaintext", "secret_value", "raw_value"])
    async def test_the_column_does_not_exist(self, conn, column: str) -> None:
        assert await column_type(conn, "secrets", column) is None

    async def test_encrypted_value_is_bytea(self, conn) -> None:
        assert await column_type(conn, "secrets", "encrypted_value") == "bytea"
