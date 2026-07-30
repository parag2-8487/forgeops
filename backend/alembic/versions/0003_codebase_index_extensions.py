# SPDX-License-Identifier: FSL-1.1-ALv2
"""Codebase index extensions: file_contents, file_dependencies, analysis_reports,
embeddings_local with its HNSW index, the cAST columns on embeddings, and pg_trgm.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-30

Design: §6.3, §6.4, §6.5, §17.1 D-48, Appendix E criterion 12.

`embeddings.embedding` stays `vector(1536)` and is not touched. D-48's whole point
is that 1024-d self-hosted vectors get their own table rather than being padded or
truncated into the 1536-d column, so this revision must be additive on `embeddings`
and additive only. The eight cAST columns are all nullable for the same reason: an
additive column with a backfill is a different kind of migration, and this one does
not need to be.

Both HNSW indexes use `vector_cosine_ops` with `m = 16, ef_construction = 64`, so
recall and latency behaviour is comparable between the two vector spaces. IVFFlat
remains rejected for production vector search (Research §0, §A0a).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMS_LOCAL = 1024

#: The cAST metadata columns added to `embeddings` (Research §C10).
CAST_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("symbol", sa.String(length=512)),
    ("parent_symbol", sa.String(length=512)),
    ("signature", sa.Text()),
    ("kind", sa.String(length=32)),
    ("start_line", sa.Integer()),
    ("end_line", sa.Integer()),
    ("token_count", sa.Integer()),
    ("chunk_metadata", sa.dialects.postgresql.JSONB()),
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # --- file_contents ------------------------------------------------------
    op.create_table(
        "file_contents",
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("redaction_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("file_id", name="pk_file_contents"),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["file_tree.id"],
            name="fk_file_contents_file_id_file_tree",
            ondelete="CASCADE",
        ),
    )

    # --- file_dependencies --------------------------------------------------
    op.create_table(
        "file_dependencies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("from_file_id", sa.Uuid(), nullable=False),
        sa.Column("to_file_id", sa.Uuid(), nullable=True),
        sa.Column("raw_specifier", sa.String(length=1024), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_file_dependencies"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_file_dependencies_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_file_id"],
            ["file_tree.id"],
            name="fk_file_dependencies_from_file_id_file_tree",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_file_id"],
            ["file_tree.id"],
            name="fk_file_dependencies_to_file_id_file_tree",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("from_file_id", "raw_specifier", name="uq_file_deps_from_specifier"),
    )
    op.create_index("ix_file_dependencies_from_file_id", "file_dependencies", ["from_file_id"])
    # The reverse lookup the incremental closure walks (Q-10). Without it, one
    # changed file costs a full-table scan.
    op.create_index("ix_file_deps_to_file", "file_dependencies", ["to_file_id"])
    # §6.3 names this index explicitly, so `project_id` deliberately carries no
    # second `index=True` index in the model: two identical indexes on one column
    # cost every write and buy no read.
    op.create_index("ix_file_deps_project", "file_dependencies", ["project_id"])

    # --- analysis_reports ---------------------------------------------------
    op.create_table(
        "analysis_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("categories", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("inventory_hash", sa.String(length=64), nullable=False),
        sa.Column("report_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_analysis_reports"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_analysis_reports_project_id_projects",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_analysis_reports_project_id", "analysis_reports", ["project_id"])
    op.create_index("ix_analysis_reports_tenant_id", "analysis_reports", ["tenant_id"])
    op.create_index(
        "ix_analysis_reports_project_id_created_at",
        "analysis_reports",
        ["project_id", "created_at"],
    )

    # --- embeddings_local (D-48) --------------------------------------------
    op.create_table(
        "embeddings_local",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("model_id", sa.String(length=100), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMS_LOCAL), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_embeddings_local"),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["file_tree.id"],
            name="fk_embeddings_local_file_id_file_tree",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("file_id", "chunk_index", name="uq_embeddings_local_file_chunk"),
    )
    op.create_index("ix_embeddings_local_file_id", "embeddings_local", ["file_id"])
    op.create_index("ix_embeddings_local_tenant_id", "embeddings_local", ["tenant_id"])
    op.execute(
        "CREATE INDEX ix_embeddings_local_embedding_hnsw "
        "ON embeddings_local USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # --- additive cAST columns on embeddings --------------------------------
    for name, type_ in CAST_COLUMNS:
        op.add_column("embeddings", sa.Column(name, type_, nullable=True))

    # --- trigram index for the file-path search the project page needs ------
    op.execute("CREATE INDEX ix_file_tree_path_trgm ON file_tree USING gin (path gin_trgm_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_file_tree_path_trgm")
    for name, _ in reversed(CAST_COLUMNS):
        op.drop_column("embeddings", name)
    op.execute("DROP INDEX IF EXISTS ix_embeddings_local_embedding_hnsw")
    op.drop_index("ix_embeddings_local_tenant_id", table_name="embeddings_local")
    op.drop_index("ix_embeddings_local_file_id", table_name="embeddings_local")
    op.drop_table("embeddings_local")
    op.drop_index("ix_analysis_reports_project_id_created_at", table_name="analysis_reports")
    op.drop_index("ix_analysis_reports_tenant_id", table_name="analysis_reports")
    op.drop_index("ix_analysis_reports_project_id", table_name="analysis_reports")
    op.drop_table("analysis_reports")
    op.drop_index("ix_file_deps_project", table_name="file_dependencies")
    op.drop_index("ix_file_deps_to_file", table_name="file_dependencies")
    op.drop_index("ix_file_dependencies_from_file_id", table_name="file_dependencies")
    op.drop_table("file_dependencies")
    op.drop_table("file_contents")
    # `pg_trgm` is not dropped: see the note in `0002`'s downgrade. `CREATE EXTENSION`
    # needs superuser, the Postgres init creates it, and `forgeops_migrator` does not
    # own it — so a DROP here would fail the whole downgrade under the §6.4 two-role
    # arrangement.
