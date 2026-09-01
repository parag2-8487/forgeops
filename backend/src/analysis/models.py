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
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Phase 0 vector dimension — matches Voyage Code 3 (D-2).
EMBEDDING_DIMS = 1536
# D-48: BGE-M3 self-hosted embeddings are 1024-d and get their own table. A pgvector
# column has one fixed dimension, and BGE-M3 is not Matryoshka-trained, so neither
# padding nor truncation is available — a second table is the only honest option.
EMBEDDING_DIMS_LOCAL = 1024


class FileTreeEntry(SQLModel, table=True):
    """Codebase file index entry. FK to Project with CASCADE delete."""

    __tablename__ = "file_tree"
    __table_args__ = (
        UniqueConstraint("project_id", "path", name="uq_file_tree_project_path"),
        # Created by revision 0003, declared here because the model must describe the
        # schema at head: `alembic check` compares the two and reports an index that
        # exists only in a migration as a pending removal on every run. This is the
        # path search the project detail page needs — `LIKE '%foo%'` cannot use a
        # B-tree, which is what makes the trigram index load-bearing rather than
        # an optimisation.
        Index(
            "ix_file_tree_path_trgm",
            "path",
            postgresql_using="gin",
            postgresql_ops={"path": "gin_trgm_ops"},
        ),
    )

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
    # --- revision 0003: cAST metadata (Research §C10) -----------------------
    # All nullable, so the migration needs no backfill. These are what turn a chunk
    # from "1200 characters of something" into "the body of Repo.Save, lines 40-78",
    # which is the difference retrieval quality actually depends on.
    symbol: str | None = Field(default=None, max_length=512)
    parent_symbol: str | None = Field(default=None, max_length=512)
    signature: str | None = Field(default=None, sa_column=Column("signature", Text, nullable=True))
    kind: str | None = Field(default=None, max_length=32)  # function|class|module|block
    start_line: int | None = Field(default=None)
    end_line: int | None = Field(default=None)
    token_count: int | None = Field(default=None)
    chunk_metadata: dict | None = Field(default=None, sa_column=Column("chunk_metadata", JSONB, nullable=True))


class FileContent(SQLModel, table=True):
    """PRD D2 `file_contents`. Holds REDACTED text only (design §6.3, §7.11).

    That is not a convention — it is what makes Q-13's cache clause enforceable.
    If the store never contains an unredacted secret, there is no unredacted source
    for a cache key to be computed from. Finding metadata (kind, path, line) lives
    in the scan report; the value lives nowhere.
    """

    __tablename__ = "file_contents"

    file_id: uuid.UUID = Field(foreign_key="file_tree.id", primary_key=True, ondelete="CASCADE")
    content: str = Field(sa_column=Column("content", Text, nullable=False))
    language: str | None = Field(default=None, max_length=64)
    summary: str | None = Field(default=None, sa_column=Column("summary", Text, nullable=True))
    redaction_count: int = Field(default=0)
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )
    )


class FileDependency(SQLModel, table=True):
    """[+] Not in PRD §7; required by phases.md §1.3's dependency-graph builder and
    by the incremental-rescan closure (Q-10).

    Unresolved specifiers are KEPT with `resolved=False` rather than dropped, so a
    later scan can resolve them without re-parsing the importer. `ix_file_deps_to_file`
    is the reverse lookup the closure walks; without it the incremental rescan
    degrades to a full-table scan per changed file.
    """

    __tablename__ = "file_dependencies"
    __table_args__ = (
        UniqueConstraint("from_file_id", "raw_specifier", name="uq_file_deps_from_specifier"),
        Index("ix_file_deps_to_file", "to_file_id"),
        Index("ix_file_deps_project", "project_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # `index=True` is deliberately absent on `project_id`: `ix_file_deps_project`
    # above already indexes it, and declaring both would create two identical
    # indexes on one column — a cost on every write for no read benefit.
    project_id: uuid.UUID = Field(foreign_key="projects.id", ondelete="CASCADE")
    from_file_id: uuid.UUID = Field(foreign_key="file_tree.id", index=True, ondelete="CASCADE")
    to_file_id: uuid.UUID | None = Field(default=None, foreign_key="file_tree.id", ondelete="SET NULL")
    raw_specifier: str = Field(max_length=1024)
    kind: str = Field(max_length=16)  # import|require|include|use
    resolved: bool = Field(default=False)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))


class AnalysisReport(SQLModel, table=True):
    """PRD D2 `analysis_reports` — §1.4's readiness score, versioned.

    `inventory_hash` is determinism evidence: two scans of the same tree must
    produce the same hash, which is what lets a readiness score be compared over
    time rather than merely displayed.
    """

    __tablename__ = "analysis_reports"
    __table_args__ = (Index("ix_analysis_reports_project_id_created_at", "project_id", "created_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    score: int = Field(sa_column=Column("score", Integer, nullable=False))
    categories: dict = Field(sa_column=Column("categories", JSONB, nullable=False))
    inventory_hash: str = Field(max_length=64)
    #: The inventory that scan produced (revision `0015`, FR-11).
    #:
    #: JSONB because its shape is the agent's versioned report schema, so a field added there must not
    #: need a migration here. `{}` means "this report predates the column", which is not the same claim as
    #: "this project has no entry points".
    #:
    #: DECLARED HERE and not only in the migration. The first attempt added the migration and missed this,
    #: and `test_alembic_autogenerate_clean.py` caught it exactly as designed: `alembic check` reported
    #: `remove_column ... analysis_reports.inventory`, because the database had a column the metadata did
    #: not. Its own message says why that matters — every such difference is a real defect, either a model
    #: change with no migration or a migration with no model change.
    inventory: dict = Field(
        default_factory=dict,
        sa_column=Column("inventory", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    report_version: int = Field(default=1)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))


class EmbeddingLocal(SQLModel, table=True):
    """D-48 — the multi-model strategy D-2 deferred to Phase 1, now decided.

    A pgvector column has one fixed dimension. Voyage Code 3 is 1536-d and BGE-M3 is
    1024-d, so a single column cannot hold both without padding or truncation, and
    BGE-M3 is not Matryoshka-trained, so truncation is not available. Phase 1
    therefore uses a SECOND TABLE, and a project reads exactly one of them, chosen by
    `projects.settings.embedding_backend`. Cross-table mixing is impossible because
    no query references both.
    """

    __tablename__ = "embeddings_local"
    __table_args__ = (
        UniqueConstraint("file_id", "chunk_index", name="uq_embeddings_local_file_chunk"),
        Index(
            "ix_embeddings_local_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    file_id: uuid.UUID = Field(foreign_key="file_tree.id", index=True, ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    chunk_index: int
    chunk_text: str = Field(sa_column=Column("chunk_text", Text, nullable=False))
    # NOT NULL provenance, per D-2. The same rule as the 1536-d table: a vector
    # whose producing model is unknown cannot be safely compared with anything.
    model_id: str = Field(max_length=100)
    embedding: list[float] = Field(sa_column=Column("embedding", Vector(EMBEDDING_DIMS_LOCAL), nullable=False))
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()))
    # --- revision 0012: the cAST metadata `embeddings` has carried since 0003 -------------
    # This table was created without them, and the omission was invisible until a scan actually
    # wrote here: `_persist_embeddings` issues ONE insert for whichever table `embedder.table`
    # names, so the first self-hosted scan failed on `column "symbol" does not exist` having
    # already paid for every vector.
    #
    # Declared here as well as in the migration for the reason the HNSW index above gives: model
    # and database must agree, or `alembic check` reports a pending removal on every run. Widths
    # match `Embedding` exactly — a `symbol` capped differently on one table would truncate the
    # same declaration differently depending on which backend a project used.
    symbol: str | None = Field(default=None, max_length=512)
    parent_symbol: str | None = Field(default=None, max_length=512)
    signature: str | None = Field(default=None, sa_column=Column("signature", Text, nullable=True))
    kind: str | None = Field(default=None, max_length=32)
    start_line: int | None = Field(default=None)
    end_line: int | None = Field(default=None)
    token_count: int | None = Field(default=None)
    chunk_metadata: dict | None = Field(default=None, sa_column=Column("chunk_metadata", JSONB, nullable=True))
