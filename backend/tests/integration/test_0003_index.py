# SPDX-License-Identifier: FSL-1.1-ALv2
"""Revision `0003_codebase_index_extensions` against a REAL PostgreSQL.

design.md §6.3, §6.4, §6.5, §17.1 D-48; Appendix E criterion 12; tasks.md leaf 5.2.

The dimension assertions read `format_type`, not `information_schema`. The latter
reports `vector` with no modifier, so it cannot tell `vector(1536)` from
`vector(1024)` — which is the entire content of D-48. The index assertions read
`pg_indexes.indexdef`, the server's own rendering, so a wrong `ef_construction` or a
missing operator class cannot pass.

The `with_ef_search` clause is the one worth reading twice. It asserts the value is
visible inside the transaction **and gone in the next transaction on the same
connection**. Only the second half distinguishes `SET LOCAL` from `SET`; a test that
checks only the first would pass against a session-level `SET` that leaks HNSW
tuning into every later query on that pooled connection.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from .migration_support import column_type, index_definition, rows, scalar

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

#: The eight additive cAST columns (Research §C10). All nullable, so `0003` needs no
#: backfill — an additive column with a backfill is a different kind of migration.
CAST_COLUMNS = (
    "symbol",
    "parent_symbol",
    "signature",
    "kind",
    "start_line",
    "end_line",
    "token_count",
    "chunk_metadata",
)

HNSW_INDEXES = {
    "ix_embeddings_embedding_hnsw": "embeddings",
    "ix_embeddings_local_embedding_hnsw": "embeddings_local",
}


class TestTheTwoVectorSpacesKeepTheirOwnDimensions:
    async def test_embeddings_is_still_1536(self, conn) -> None:
        assert await column_type(conn, "embeddings", "embedding") == "vector(1536)"

    async def test_embeddings_local_is_1024(self, conn) -> None:
        assert await column_type(conn, "embeddings_local", "embedding") == "vector(1024)"

    async def test_the_dimensions_differ(self, conn) -> None:
        """D-48 in one line: two tables exist precisely because one column cannot
        hold both, and BGE-M3 is not Matryoshka-trained so truncation is unavailable."""
        wide = await column_type(conn, "embeddings", "embedding")
        narrow = await column_type(conn, "embeddings_local", "embedding")
        assert wide != narrow

    @pytest.mark.parametrize("table", ["embeddings", "embeddings_local"])
    async def test_model_id_is_not_null(self, conn, table: str) -> None:
        """D-2's provenance rule. A vector whose producing model is unknown cannot be
        safely compared with anything."""
        notnull = await scalar(
            conn,
            """
            SELECT a.attnotnull
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            WHERE c.relname = :table AND a.attname = 'model_id'
            """,
            table=table,
        )
        assert notnull is True


class TestBothHnswIndexes:
    @pytest.mark.parametrize("index", sorted(HNSW_INDEXES))
    async def test_the_index_exists(self, conn, index: str) -> None:
        assert await index_definition(conn, index) is not None

    @pytest.mark.parametrize("index", sorted(HNSW_INDEXES))
    async def test_the_index_uses_hnsw_and_cosine(self, conn, index: str) -> None:
        definition = await index_definition(conn, index)
        assert definition is not None
        assert "USING hnsw" in definition, definition
        assert "vector_cosine_ops" in definition, definition

    @pytest.mark.parametrize("index", sorted(HNSW_INDEXES))
    async def test_the_build_parameters_are_exact(self, conn, index: str) -> None:
        """Read from the server's own rendering, so a wrong number cannot pass."""
        definition = await index_definition(conn, index)
        assert definition is not None
        normalised = definition.replace(" ", "")
        assert "m='16'" in normalised, definition
        assert "ef_construction='64'" in normalised, definition

    async def test_neither_index_is_ivfflat(self, conn) -> None:
        """IVFFlat is explicitly rejected for production vector search
        (Research §0, §A0a)."""
        for index in HNSW_INDEXES:
            definition = await index_definition(conn, index)
            assert definition is not None
            assert "ivfflat" not in definition.lower(), definition


class TestTheReverseDependencyIndex:
    async def test_it_exists_on_to_file_id(self, conn) -> None:
        """The incremental closure (Q-10) walks importers *backwards* from a changed
        file. Without this index that walk is a full-table scan per changed file."""
        definition = await index_definition(conn, "ix_file_deps_to_file")
        assert definition is not None
        assert "to_file_id" in definition, definition

    async def test_project_id_is_indexed_exactly_once(self, conn) -> None:
        """§6.3 names `ix_file_deps_project` explicitly, so the model deliberately
        omits a second `index=True` index on the same column. Two identical indexes
        cost every write and buy no read."""
        found = await rows(
            conn,
            """
            SELECT indexname, indexdef FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'file_dependencies'
            """,
        )
        on_project_only = [name for name, definition in found if "(project_id)" in definition.replace(" ", "")]
        assert on_project_only == ["ix_file_deps_project"], on_project_only


class TestTheTrigramIndex:
    async def test_it_exists_and_uses_gin_trgm_ops(self, conn) -> None:
        definition = await index_definition(conn, "ix_file_tree_path_trgm")
        assert definition is not None
        assert "gin" in definition.lower(), definition
        assert "gin_trgm_ops" in definition, definition

    async def test_pg_trgm_is_installed(self, conn) -> None:
        assert await scalar(conn, "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'") == 1


class TestTheAdditiveCastColumns:
    @pytest.mark.parametrize("column", CAST_COLUMNS)
    async def test_the_column_exists(self, conn, column: str) -> None:
        assert await column_type(conn, "embeddings", column) is not None

    @pytest.mark.parametrize("column", CAST_COLUMNS)
    async def test_the_column_is_nullable(self, conn, column: str) -> None:
        notnull = await scalar(
            conn,
            """
            SELECT a.attnotnull
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            WHERE c.relname = 'embeddings' AND a.attname = :column
            """,
            column=column,
        )
        assert notnull is False, (
            f"embeddings.{column} is NOT NULL; every cAST column is nullable so 0003 needs no backfill (design §6.3)"
        )

    async def test_chunk_metadata_is_jsonb(self, conn) -> None:
        assert await column_type(conn, "embeddings", "chunk_metadata") == "jsonb"


class TestTheNewIndexTables:
    async def test_file_contents_is_keyed_on_file_id(self, conn) -> None:
        primary = await rows(
            conn,
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE c.relname = 'file_contents' AND i.indisprimary
            """,
        )
        assert [r[0] for r in primary] == ["file_id"]

    async def test_analysis_reports_accepts_a_score_and_categories(self, conn) -> None:
        from .migration_support import make_project

        project_id = await make_project(conn, "readiness")
        report_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO analysis_reports "
                "(id, project_id, score, categories, inventory_hash, report_version) "
                "VALUES (:id, :p, 72, :cats, :hash, 1)"
            ),
            {
                "id": report_id,
                "p": project_id,
                "cats": '{"containerisation": 20, "ci_cd": 12}',
                "hash": "a" * 64,
            },
        )
        score = await scalar(conn, "SELECT score FROM analysis_reports WHERE id = :id", id=report_id)
        assert score == 72


class TestEfSearchIsTransactionScopedOnly:
    async def test_the_value_applies_inside_and_is_gone_in_the_next_transaction(self, head_engine) -> None:
        """The second half is the assertion. A session-level `SET` would satisfy the
        first half and then leak HNSW tuning into every later query on the same
        pooled connection — which is exactly what `SET LOCAL` exists to prevent."""
        from sqlalchemy.ext.asyncio import AsyncSession
        from src.core.db import with_ef_search

        async with head_engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(bind=connection)
            await with_ef_search(session, 128)
            inside = await connection.execute(text("SELECT current_setting('hnsw.ef_search', true)"))
            assert inside.scalar() == "128"
            await transaction.rollback()

            # A NEW transaction on the SAME connection.
            await connection.begin()
            after = await connection.execute(text("SELECT current_setting('hnsw.ef_search', true)"))
            leaked = after.scalar()
            assert leaked != "128", (
                f"hnsw.ef_search survived the transaction as {leaked!r}; with_ef_search "
                f"must use SET LOCAL, never a session-level SET (design §6.7)"
            )
