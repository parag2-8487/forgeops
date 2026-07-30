# SPDX-License-Identifier: FSL-1.1-ALv2
"""Shared plumbing for the revision proofs `test_0002` … `test_0009` (§6.5).

Every revision in §6.5 is proven by a gated integration test against a real
PostgreSQL, in the D-26 pattern: `require_capability("postgres")` skips locally and
**fails** when `FORGEOPS_REQUIRE_INTEGRATION=1`. Nine test modules would otherwise
each carry their own copy of the Alembic subprocess helper and the fixture that
brings the database to head, so the copies live here once.

Two choices are deliberate.

**Alembic runs in a subprocess.** `alembic/env.py` drives async migrations with
`asyncio.run(...)`, which raises "cannot be called from a running event loop" if
invoked in-process from an async test. Shelling out also exercises exactly the
command path an operator and CI use, rather than a Python API nobody runs.

**The fixture is session-scoped and read-only.** Bringing the schema to head costs
several seconds; doing it per test module would multiply that by nine. Each proof
therefore treats the schema as read-only and creates the rows it needs inside a
transaction it rolls back, which is also what keeps the proofs order-independent.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]


def run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real Alembic CLI in a child process."""
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    # The migration messages contain section signs; a cp1252 console would raise
    # UnicodeEncodeError on Windows and mask the actual result.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def alembic_ok(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    result = run_alembic(database_url, *args)
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed ({result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


@pytest.fixture(scope="session")
def schema_at_head(database_url: str) -> Iterator[str]:
    """Bring the real database to head once for the whole session.

    Not torn down to base afterwards: the next session starts with
    `downgrade base`, so the end state of a run is a fully migrated database an
    operator can inspect after a failure. That is more useful than an empty one.
    """
    alembic_ok(database_url, "downgrade", "base")
    alembic_ok(database_url, "upgrade", "head")
    yield database_url


@pytest_asyncio.fixture()
async def head_engine(schema_at_head: str) -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test.

    Deliberately function-scoped even though `schema_at_head` is session-scoped.
    pytest-asyncio gives each test its own event loop by default, and an asyncpg
    connection pool bound to a closed loop raises `RuntimeError: Event loop is
    closed` on teardown — which surfaces as every test in the module failing for a
    reason unrelated to what it asserts. Engine construction is microseconds; the
    expensive part is the migration run, and that stays session-scoped.
    """
    engine = create_async_engine(schema_at_head, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def conn(head_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection inside a transaction that is always rolled back.

    Every proof that inserts rows uses this, so no proof can observe another's
    writes and the order they run in cannot matter.
    """
    async with head_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


async def scalar(connection: AsyncConnection, sql: str, **params: object) -> object:
    result = await connection.execute(text(sql), params)
    return result.scalar()


async def rows(connection: AsyncConnection, sql: str, **params: object) -> list[tuple]:
    result = await connection.execute(text(sql), params)
    return [tuple(r) for r in result]


async def fk_delete_action(connection: AsyncConnection, constraint: str) -> str | None:
    """The declared `ON DELETE` action of a foreign key, as a one-letter code.

    `pg_constraint.confdeltype` is Postgres's internal `"char"` type, which asyncpg
    hands back as `bytes` — so a naive `== "c"` compares `b'c'` to `'c'` and fails
    for a reason that has nothing to do with the schema. The cast belongs here, once.

    Returns `'a'` NO ACTION, `'r'` RESTRICT, `'c'` CASCADE, `'n'` SET NULL,
    `'d'` SET DEFAULT, or `None` when the constraint does not exist.
    """
    value = await scalar(
        connection,
        "SELECT confdeltype::text FROM pg_constraint WHERE conname = :name",
        name=constraint,
    )
    return None if value is None else str(value)


async def index_definition(connection: AsyncConnection, index: str) -> str | None:
    """`pg_indexes.indexdef` — the server's own rendering of an index.

    Asserting against this rather than against `pg_index` flags means the build
    parameters and the operator class are checked as the server actually stored
    them, which is the only form that cannot pass with a wrong `ef_construction`.
    """
    value = await scalar(
        connection,
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name",
        name=index,
    )
    return None if value is None else str(value)


async def column_type(connection: AsyncConnection, table: str, column: str) -> str | None:
    """`format_type` — the real server-side type including any modifier.

    `information_schema` reports `vector` without its dimension, so it cannot tell
    `vector(1536)` from `vector(1024)`; this can.
    """
    value = await scalar(
        connection,
        """
        SELECT format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = :table AND a.attname = :column
          AND a.attnum > 0 AND NOT a.attisdropped
        """,
        table=table,
        column=column,
    )
    return None if value is None else str(value)


async def make_project(connection: AsyncConnection, name: str = "proof") -> uuid.UUID:
    """Insert a minimal project and return its id."""
    project_id = uuid.uuid4()
    await connection.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
        {"id": project_id, "name": f"{name}-{project_id.hex[:8]}", "path": f"/tmp/{name}"},
    )
    return project_id


async def make_user(connection: AsyncConnection, role: str = "developer") -> uuid.UUID:
    """Insert a minimal user and return its id.

    The email and IdP subject are derived from a fresh UUID so repeated calls in one
    transaction cannot collide on `uq_users_email` or `uq_users_idp_subject`.
    """
    user_id = uuid.uuid4()
    await connection.execute(
        text(
            "INSERT INTO users (id, email, name, role, idp_subject, is_active) "
            "VALUES (:id, :email, :name, :role, :sub, true)"
        ),
        {
            "id": user_id,
            "email": f"proof-{user_id.hex[:12]}@example.invalid",
            "name": "Proof User",
            "role": role,
            "sub": f"sub-{user_id.hex}",
        },
    )
    return user_id


async def make_file(connection: AsyncConnection, project_id: uuid.UUID, path: str) -> uuid.UUID:
    """Insert a minimal file_tree row and return its id."""
    file_id = uuid.uuid4()
    await connection.execute(
        text(
            "INSERT INTO file_tree (id, project_id, path, content_hash, size_bytes, "
            "last_modified, created_at) VALUES (:id, :project_id, :path, :hash, 1, now(), now())"
        ),
        {"id": file_id, "project_id": project_id, "path": path, "hash": "0" * 64},
    )
    return file_id
