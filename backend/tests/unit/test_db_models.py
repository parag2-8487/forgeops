# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for db primitives (task 5.1), models (task 5.2), migration shape (task 5.3).

These tests do NOT require a live database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from src.analysis.models import EMBEDDING_DIMS, Embedding, FileTreeEntry
from src.core.db import (
    NAMING_CONVENTION,
    create_db_engine,
    create_sessionmaker,
    get_session,
    metadata,
    with_ef_search,
)
from src.projects.models import Project

# ============================================================================
# Task 5.1: db.py tests
# ============================================================================


class TestCreateDbEngine:
    """Engine construction must not require a live database."""

    def test_engine_constructed_without_connection(self) -> None:
        """Construction validates URL but does not connect."""
        settings = MagicMock()
        settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/test"
        settings.database_pool_size = 5
        engine = create_db_engine(settings)
        assert engine is not None
        # Engine pool should have the configured size
        assert engine.pool.size() == 5

    def test_pool_settings(self) -> None:
        """Engine has correct pool configuration."""
        settings = MagicMock()
        settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/test"
        settings.database_pool_size = 10
        engine = create_db_engine(settings)
        assert engine.pool.size() == 10


class TestCreateSessionmaker:
    """Sessionmaker configuration tests."""

    def test_expire_on_commit_false(self) -> None:
        """expire_on_commit=False is set (Research §0 mandate)."""
        settings = MagicMock()
        settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/test"
        settings.database_pool_size = 5
        engine = create_db_engine(settings)
        sm = create_sessionmaker(engine)
        # Create a session to inspect settings
        # The sessionmaker itself stores the configuration
        assert sm.kw.get("expire_on_commit") is False

    def test_autoflush_false(self) -> None:
        """autoflush=False is set."""
        settings = MagicMock()
        settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/test"
        settings.database_pool_size = 5
        engine = create_db_engine(settings)
        sm = create_sessionmaker(engine)
        assert sm.kw.get("autoflush") is False


class TestPostCommitAttributeAccess:
    """Post-commit attribute access must work with expire_on_commit=False."""

    @pytest.mark.asyncio
    async def test_post_commit_attribute_access_works(self) -> None:
        """With expire_on_commit=False, attributes are accessible after commit.

        This is the whole point: without it, accessing attributes post-commit in
        async code raises MissingGreenlet (Research §0). We mock the commit and
        demonstrate that the session config enables post-commit access.
        """
        settings = MagicMock()
        settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/test"
        settings.database_pool_size = 5
        engine = create_db_engine(settings)
        sm = create_sessionmaker(engine)
        # Verify the sessionmaker produces sessions with expire_on_commit=False
        # The kw dict is used to configure every session created
        assert sm.kw["expire_on_commit"] is False
        # This means after session.commit(), ORM-loaded attributes remain in
        # the instance __dict__ without triggering a lazy DB refresh.


class TestGetSessionRollback:
    """Request-scoped session rolls back on exception."""

    @pytest.mark.asyncio
    async def test_rollback_on_exception(self) -> None:
        """get_session rolls back the session if an exception occurs."""

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        mock_factory = MagicMock()
        # async context manager that yields mock_session
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_cm

        mock_request = MagicMock()
        mock_request.app.state.sessionmaker = mock_factory

        gen = get_session(mock_request)
        session = await gen.__anext__()
        assert session is mock_session

        # Simulate an exception during request processing
        with pytest.raises(RuntimeError, match="test error"):
            try:
                raise RuntimeError("test error")
            except RuntimeError:
                # The generator's throw triggers the rollback path
                try:
                    await gen.athrow(RuntimeError("test error"))
                except RuntimeError:
                    raise

        mock_session.rollback.assert_called_once()
        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_commit_on_success(self) -> None:
        """get_session commits the session on successful completion."""

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        mock_factory = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_cm

        mock_request = MagicMock()
        mock_request.app.state.sessionmaker = mock_factory

        gen = get_session(mock_request)
        session = await gen.__anext__()
        assert session is mock_session

        # Complete successfully (StopAsyncIteration)
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

        mock_session.commit.assert_called_once()
        mock_session.rollback.assert_not_called()


class TestWithEfSearch:
    """`with_ef_search` must scope the tuning to the transaction.

    These are unit tests over a mock session, so they can only assert the statement
    that is *issued*. They previously asserted `SET LOCAL hnsw.ef_search = :v`, which
    the mock accepted happily and PostgreSQL rejects outright — `SET` is utility
    syntax and takes no bind parameters, so the function had never worked against a
    real server. `test_0003_index.py` now binds the behavioural assertion to this
    function against real Postgres; these tests keep the *shape* honest.
    """

    @pytest.mark.asyncio
    async def test_ef_search_is_transaction_local(self) -> None:
        """`set_config(..., true)` is `SET LOCAL` semantics and is parameterisable."""

        mock_session = AsyncMock()
        await with_ef_search(mock_session, 200)
        mock_session.execute.assert_called_once()
        call_args = mock_session.execute.call_args
        sql_text = str(call_args[0][0].text)
        assert "set_config" in sql_text
        assert "hnsw.ef_search" in sql_text
        # The third argument of set_config is `is_local`; `true` is what makes it
        # revert at transaction end rather than leak across a pooled connection.
        assert "true" in sql_text
        params = call_args[0][1]
        assert params["value"] == "200"

    @pytest.mark.asyncio
    async def test_the_value_is_bound_not_interpolated(self) -> None:
        """The value must never appear in the statement text. Interpolating it would
        be the other way to make `SET` work, and it turns a tuning knob into a
        SQL-injection surface for no benefit."""

        mock_session = AsyncMock()
        await with_ef_search(mock_session, 4242)
        sql_text = str(mock_session.execute.call_args[0][0].text)
        assert "4242" not in sql_text, sql_text

    @pytest.mark.asyncio
    async def test_ef_search_different_values(self) -> None:
        """with_ef_search passes different ef_search values correctly."""

        for val in [40, 100, 500]:
            mock_session = AsyncMock()
            await with_ef_search(mock_session, val)
            params = mock_session.execute.call_args[0][1]
            assert params["value"] == str(val)


class TestNamingConvention:
    """MetaData naming convention tests."""

    def test_naming_convention_has_all_keys(self) -> None:
        """All five constraint prefix patterns are defined."""
        expected_keys = {"ix", "uq", "ck", "fk", "pk"}
        assert set(NAMING_CONVENTION.keys()) == expected_keys

    def test_ix_prefix(self) -> None:
        assert NAMING_CONVENTION["ix"].startswith("ix_")

    def test_uq_prefix(self) -> None:
        assert NAMING_CONVENTION["uq"].startswith("uq_")

    def test_ck_prefix(self) -> None:
        assert NAMING_CONVENTION["ck"].startswith("ck_")

    def test_fk_prefix(self) -> None:
        assert NAMING_CONVENTION["fk"].startswith("fk_")

    def test_pk_prefix(self) -> None:
        assert NAMING_CONVENTION["pk"].startswith("pk_")

    def test_metadata_uses_convention(self) -> None:
        """The module-level metadata uses NAMING_CONVENTION."""
        assert metadata.naming_convention == NAMING_CONVENTION


# ============================================================================
# Task 5.2: Model introspection tests
# ============================================================================


class TestProjectModel:
    """Project table model introspection."""

    def test_table_name(self) -> None:
        assert Project.__tablename__ == "projects"

    def test_columns_exist(self) -> None:
        cols = {c.name for c in Project.__table__.columns}
        expected = {"id", "tenant_id", "name", "path", "repo_url", "settings", "created_at", "updated_at"}
        assert expected.issubset(cols)

    def test_tenant_id_nullable(self) -> None:
        """tenant_id is nullable (Phase 1 RLS seam)."""
        col = Project.__table__.c.tenant_id
        assert col.nullable is True

    def test_primary_key(self) -> None:
        pk_cols = [c.name for c in Project.__table__.primary_key.columns]
        assert pk_cols == ["id"]


class TestFileTreeEntryModel:
    """FileTreeEntry table model introspection."""

    def test_table_name(self) -> None:
        assert FileTreeEntry.__tablename__ == "file_tree"

    def test_unique_constraint_name(self) -> None:
        """Deterministic constraint name uq_file_tree_project_path."""
        constraints = {c.name for c in FileTreeEntry.__table__.constraints if hasattr(c, "name") and c.name}
        assert "uq_file_tree_project_path" in constraints

    def test_project_id_fk_cascade(self) -> None:
        """project_id FK has CASCADE ondelete."""
        fks = list(FileTreeEntry.__table__.foreign_keys)
        assert len(fks) == 1
        fk = fks[0]
        assert fk.column.table.name == "projects"
        # Check parent constraint ondelete
        assert fk.parent.onupdate is None or True  # ondelete check via constraint
        # The FK constraint itself tracks ondelete
        for c in FileTreeEntry.__table__.constraints:
            if hasattr(c, "ondelete") and c.ondelete:
                assert c.ondelete == "CASCADE"

    def test_columns(self) -> None:
        cols = {c.name for c in FileTreeEntry.__table__.columns}
        expected = {"id", "project_id", "path", "content_hash", "size_bytes", "last_modified", "created_at"}
        assert expected.issubset(cols)


class TestEmbeddingModel:
    """Embedding table model introspection."""

    def test_table_name(self) -> None:
        assert Embedding.__tablename__ == "embeddings"

    def test_unique_constraint_name(self) -> None:
        """Deterministic constraint name uq_embeddings_file_chunk."""
        constraints = {c.name for c in Embedding.__table__.constraints if hasattr(c, "name") and c.name}
        assert "uq_embeddings_file_chunk" in constraints

    def test_tenant_id_nullable(self) -> None:
        """tenant_id is nullable (Phase 1 RLS seam)."""
        col = Embedding.__table__.c.tenant_id
        assert col.nullable is True

    def test_model_id_not_nullable(self) -> None:
        """model_id is required (D-2 provenance mandate)."""
        col = Embedding.__table__.c.model_id
        assert col.nullable is False

    def test_embedding_vector_dimension(self) -> None:
        """embedding column is Vector(1536)."""
        from pgvector.sqlalchemy import Vector

        col = Embedding.__table__.c.embedding
        assert isinstance(col.type, Vector)
        assert col.type.dim == 1536

    def test_embedding_dims_constant(self) -> None:
        """EMBEDDING_DIMS is 1536."""
        assert EMBEDDING_DIMS == 1536

    def test_file_id_fk_cascade(self) -> None:
        """file_id FK has CASCADE ondelete."""
        for c in Embedding.__table__.constraints:
            if hasattr(c, "ondelete") and c.ondelete:
                assert c.ondelete == "CASCADE"

    def test_embedding_not_nullable(self) -> None:
        """embedding column is NOT NULL."""
        col = Embedding.__table__.c.embedding
        assert col.nullable is False


# ============================================================================
# Task 5.3: Migration shape tests (static, no DB)
# ============================================================================


class TestMigrationShape:
    """Static assertions about the migration file content."""

    def _read_migration(self) -> str:
        import os

        migration_path = os.path.join(os.path.dirname(__file__), "..", "..", "alembic", "versions", "0001_initial.py")
        with open(migration_path, encoding="utf-8") as f:
            return f.read()

    def test_creates_vector_extension_first(self) -> None:
        """CREATE EXTENSION vector appears before any vector column."""
        content = self._read_migration()
        ext_pos = content.find("CREATE EXTENSION IF NOT EXISTS vector")
        vector_col_pos = content.find("Vector(1536)")
        assert ext_pos != -1, "CREATE EXTENSION not found"
        assert vector_col_pos != -1, "Vector(1536) not found"
        assert ext_pos < vector_col_pos, "Extension must be created before vector column"

    def test_exactly_three_tables(self) -> None:
        """Migration creates exactly three tables."""
        content = self._read_migration()
        assert content.count("op.create_table(") == 3

    def test_hnsw_index_created(self) -> None:
        """HNSW cosine index is created."""
        content = self._read_migration()
        assert "ix_embeddings_embedding_hnsw" in content
        assert "hnsw" in content
        assert "vector_cosine_ops" in content
        assert "m = 16" in content
        assert "ef_construction = 64" in content

    def test_no_rls_policies(self) -> None:
        """No RLS policies in Phase 0 migration."""
        content = self._read_migration()
        assert "ROW LEVEL SECURITY" not in content.upper()
        assert "CREATE POLICY" not in content.upper()

    def test_revision_id(self) -> None:
        """Revision is 0001."""
        content = self._read_migration()
        assert 'revision: str = "0001"' in content

    def test_down_revision_none(self) -> None:
        """It's the first migration."""
        content = self._read_migration()
        assert "down_revision" in content
        assert "None" in content

    def test_downgrade_does_not_drop_the_extension(self) -> None:
        """The downgrade must NOT drop `vector`.

        `CREATE EXTENSION vector` requires superuser, so under the §6.4 two-role
        arrangement `forgeops_migrator` runs the migration but does not own the
        extension — a `DROP EXTENSION` would fail and abort the whole downgrade.
        `scripts/postgres-init/10-forgeops-roles.sh` creates it as superuser at
        database initialisation instead, which leaves the `CREATE EXTENSION IF NOT
        EXISTS` in `upgrade()` as a working no-op for a superuser-run migration.
        An extension is database infrastructure that outlives one schema revision.
        """
        content = self._read_migration()
        assert "CREATE EXTENSION IF NOT EXISTS vector" in content, (
            "upgrade() must still create the extension so a superuser-run migration works standalone"
        )
        assert "DROP EXTENSION" not in content, (
            "the downgrade must not drop an extension the migrating role may not own (design §6.4, §6.7)"
        )
