# SPDX-License-Identifier: FSL-1.1-ALv2
"""FileTreeEntry and Embedding SQLModel tables (design.md §6.2).

SETTLED by decision D-2 (§17.1): the Phase 0 vector column is fixed at 1536
dimensions — Voyage Code 3, the primary API embedding model (Research §C10).
BGE-M3's 1024-d self-hosted vectors are NOT stored in this column; the
multi-model strategy (second table per dimension, or Matryoshka truncation to a
common size) is decided in Phase 1. Every row carries model_id for provenance.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Column, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

# Phase 0 vector dimension — matches Voyage Code 3 (D-2).
EMBEDDING_DIMS = 1536


class FileTreeEntry(SQLModel, table=True):
    """Codebase file index entry. FK to Project with CASCADE delete."""

    __tablename__ = "file_tree"
    __table_args__ = (UniqueConstraint("project_id", "path", name="uq_file_tree_project_path"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    path: str = Field(max_length=1024)
    content_hash: str = Field(max_length=64)
    # design.md §6.2 types this column `bigint`; the default Python int maps to
    # INTEGER, which would silently cap file sizes at 2 GiB and would also make
    # `alembic check` report drift against 0001_initial.
    size_bytes: int = Field(sa_column=Column("size_bytes", BigInteger, nullable=False))
    last_modified: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Embedding(SQLModel, table=True):
    """Vector embedding for a file chunk. FK to FileTreeEntry with CASCADE delete."""

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("file_id", "chunk_index", name="uq_embeddings_file_chunk"),
        # The HNSW index is declared in the model metadata as well as in
        # 0001_initial so that model and database agree: an index that exists
        # only in the migration is reported by `alembic check` as a pending
        # removal on every run (design.md §6.3, §6.4).
        Index(
            "ix_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    file_id: uuid.UUID = Field(foreign_key="file_tree.id", index=True, ondelete="CASCADE")
    # Seam for Phase 1 PostgreSQL RLS. Nullable now.
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    chunk_index: int
    # design.md §6.2 types this column `text`: chunk bodies are unbounded, and a
    # VARCHAR here would also register as drift against 0001_initial.
    chunk_text: str = Field(sa_column=Column("chunk_text", Text, nullable=False))
    # Provenance, mandated by D-2: which model produced this vector. Required so a
    # Phase 1 multi-model strategy can distinguish 1536-d from 1024-d sources.
    model_id: str = Field(max_length=100)  # e.g. "voyage-code-3"
    embedding: list[float] = Field(sa_column=Column("embedding", Vector(EMBEDDING_DIMS), nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow)
