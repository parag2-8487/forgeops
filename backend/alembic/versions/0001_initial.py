# SPDX-License-Identifier: FSL-1.1-ALv2
"""Initial schema: projects, file_tree, embeddings with pgvector HNSW index.

Revision ID: 0001
Revises:
Create Date: 2026-07-26

Design: §6.2–§6.4. Exactly ONE migration for Phase 0.
- CREATE EXTENSION vector BEFORE any vector column.
- Three tables: projects, file_tree, embeddings.
- HNSW cosine index on embeddings.embedding with m=16, ef_construction=64.
- No RLS policies (Phase 1).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector extension MUST be created before any vector column.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- projects ---
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("repo_url", sa.String(length=1024), nullable=True),
        sa.Column(
            "settings",
            sa.dialects.postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
    )
    op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"])
    op.create_index("ix_projects_name", "projects", ["name"])

    # --- file_tree ---
    op.create_table(
        "file_tree",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("last_modified", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_file_tree"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_file_tree_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "path", name="uq_file_tree_project_path"),
    )
    op.create_index("ix_file_tree_project_id", "file_tree", ["project_id"])

    # --- embeddings ---
    op.create_table(
        "embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("model_id", sa.String(length=100), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_embeddings"),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["file_tree.id"],
            name="fk_embeddings_file_id_file_tree",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("file_id", "chunk_index", name="uq_embeddings_file_chunk"),
    )
    op.create_index("ix_embeddings_file_id", "embeddings", ["file_id"])
    op.create_index("ix_embeddings_tenant_id", "embeddings", ["tenant_id"])

    # HNSW cosine index for vector similarity search (design.md §6.3, D-2).
    # IVFFlat is explicitly rejected for production vector search (Research §0, §A0a).
    op.execute(
        "CREATE INDEX ix_embeddings_embedding_hnsw "
        "ON embeddings USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.drop_index("ix_embeddings_embedding_hnsw", table_name="embeddings")
    op.drop_index("ix_embeddings_tenant_id", table_name="embeddings")
    op.drop_index("ix_embeddings_file_id", table_name="embeddings")
    op.drop_table("embeddings")
    op.drop_index("ix_file_tree_project_id", table_name="file_tree")
    op.drop_table("file_tree")
    op.drop_index("ix_projects_name", table_name="projects")
    op.drop_index("ix_projects_tenant_id", table_name="projects")
    op.drop_table("projects")
    op.execute("DROP EXTENSION IF EXISTS vector")
