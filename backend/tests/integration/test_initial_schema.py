# SPDX-License-Identifier: FSL-1.1-ALv2
"""Task 5.4 — the initial schema against a REAL PostgreSQL (design.md §6.2–§6.4).

Asserts, against a live server rather than a model introspection:
  * `alembic upgrade head` succeeds and installs the `vector` extension;
  * exactly the three Phase 0 tables exist;
  * `embeddings.embedding` really is `vector(1536)` (D-2);
  * `embeddings.model_id` is NOT NULL (the D-2 provenance column);
  * the HNSW index exists and uses `vector_cosine_ops` with m=16/ef_construction=64;
  * a transaction-local `SET LOCAL hnsw.ef_search` round trip works and reverts;
  * `alembic check` reports NO pending model/database difference, which is what
    proves the pgvector `render_item` hook stops the spurious drop/recreate;
  * `alembic downgrade base` leaves no Phase 0 table behind.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {"projects", "file_tree", "embeddings"}
HNSW_INDEX = "ix_embeddings_embedding_hnsw"

pytestmark = pytest.mark.asyncio


def run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real Alembic CLI in a child process.

    A subprocess is used deliberately: alembic/env.py drives async migrations
    with `asyncio.run(...)`, which raises "cannot be called from a running event
    loop" if the command is invoked in-process from an async test. Shelling out
    also exercises exactly the command path an operator and CI use.
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )


def alembic_ok(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    result = run_alembic(database_url, *args)
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed ({result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


@pytest.fixture()
def migrated(database_url: str):
    """Bring the real database to head, then tear it back down to base."""
    alembic_ok(database_url, "downgrade", "base")
    alembic_ok(database_url, "upgrade", "head")
    yield database_url
    alembic_ok(database_url, "downgrade", "base")


async def _scalar(engine, sql: str, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


class TestInitialSchemaAgainstPostgres:
    async def test_vector_extension_installed(self, migrated, database_url: str) -> None:
        engine = create_async_engine(database_url)
        try:
            version = await _scalar(engine, "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        finally:
            await engine.dispose()
        assert version is not None, "0001_initial must CREATE EXTENSION vector"

    async def test_exactly_the_three_phase_0_tables(self, migrated, database_url: str) -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                    )
                )
                tables = {r[0] for r in rows}
        finally:
            await engine.dispose()
        assert tables == EXPECTED_TABLES, (
            f"Phase 0 defines exactly three tables (design.md §6.1); found {sorted(tables)}"
        )

    async def test_embedding_column_is_vector_1536(self, migrated, database_url: str) -> None:
        engine = create_async_engine(database_url)
        try:
            # format_type renders the real server-side type, including the
            # dimension modifier, so this cannot pass with a wrong dimension.
            rendered = await _scalar(
                engine,
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a "
                "WHERE a.attrelid = 'embeddings'::regclass AND a.attname = 'embedding'",
            )
            not_null = await _scalar(
                engine,
                "SELECT a.attnotnull FROM pg_attribute a "
                "WHERE a.attrelid = 'embeddings'::regclass AND a.attname = 'model_id'",
            )
        finally:
            await engine.dispose()
        assert rendered == "vector(1536)", f"expected vector(1536), got {rendered!r}"
        assert not_null is True, "model_id is the D-2 provenance column and must be NOT NULL"

    async def test_hnsw_cosine_index_exists_with_tuned_parameters(self, migrated, database_url: str) -> None:
        engine = create_async_engine(database_url)
        try:
            definition = await _scalar(
                engine,
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'embeddings' AND indexname = :name",
                name=HNSW_INDEX,
            )
            method = await _scalar(
                engine,
                "SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam WHERE c.relname = :name",
                name=HNSW_INDEX,
            )
        finally:
            await engine.dispose()
        assert definition is not None, f"{HNSW_INDEX} must exist (design.md §6.3)"
        assert method == "hnsw", f"IVFFlat is rejected for Phase 0; got {method!r}"
        assert "vector_cosine_ops" in definition
        assert "m='16'" in definition or "m=16" in definition, definition
        assert "ef_construction='64'" in definition or "ef_construction=64" in definition, definition

    async def test_ef_search_is_transaction_scoped(self, migrated, database_url: str) -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                # pgvector registers the `hnsw.ef_search` GUC when its shared
                # library is first loaded into the session, so a bare
                # `SHOW hnsw.ef_search` on a fresh connection raises
                # "unrecognized configuration parameter". Touching a vector value
                # loads the module, which is exactly what a real query would do.
                await conn.execute(text("SELECT '[1,2,3]'::vector"))
                default = (await conn.execute(text("SHOW hnsw.ef_search"))).scalar()
                # The reads above autobegan a transaction; close it so the
                # explicit block below is a genuinely new transaction boundary.
                await conn.rollback()
                async with conn.begin():
                    await conn.execute(text("SET LOCAL hnsw.ef_search = 128"))
                    inside = (await conn.execute(text("SHOW hnsw.ef_search"))).scalar()
                after = (await conn.execute(text("SHOW hnsw.ef_search"))).scalar()
        finally:
            await engine.dispose()
        assert inside == "128", "SET LOCAL must apply inside the transaction"
        assert after == default, (
            "SET LOCAL must revert at transaction end; a session-level SET would "
            "leak across PgBouncer-pooled connections (design.md §6.5)"
        )

    async def test_autogenerate_reports_no_pending_diff(self, migrated, database_url: str) -> None:
        """A clean tree must produce no model/database difference.

        This is the assertion that actually protects against the pgvector
        autogenerate defect: without env.py's render_item registration the Vector
        column is re-detected on every run and `alembic check` fails.
        """
        result = run_alembic(database_url, "check")
        assert result.returncode == 0, (
            f"alembic check found unexpected schema drift:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    async def test_downgrade_removes_every_phase_0_table(self, database_url: str) -> None:
        alembic_ok(database_url, "downgrade", "base")
        alembic_ok(database_url, "upgrade", "head")
        alembic_ok(database_url, "downgrade", "base")
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                    )
                )
                remaining = {r[0] for r in rows}
        finally:
            await engine.dispose()
        assert remaining == set(), f"downgrade left tables behind: {sorted(remaining)}"
