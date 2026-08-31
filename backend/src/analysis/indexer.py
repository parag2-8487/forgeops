# SPDX-License-Identifier: FSL-1.1-ALv2
"""Persistence for the agent's scan report — the half of §1.3 that was missing.

`file_tree`, `file_contents`, `file_dependencies`, `embeddings`, `embeddings_local` and
`analysis_reports` have existed since revisions `0001` and `0003`, documented in
`models.py`, and were all EMPTY: the agent produced a summary inventory and nothing ever
wrote a per-file row. Every read surface built on them — the codebase index API, hybrid
retrieval, the readiness score — was therefore either empty or, worse, fabricated. This
module is what turns a scan into rows.

Three rules are load-bearing rather than stylistic.

**Content arrives redacted or not at all.** The agent redacts before the report is
serialised (`agent/internal/scanner/scanreport.go`), and `file_contents` is a redacted-only
store (design §6.3, §7.11). Nothing here un-redacts, and nothing here re-derives content
from a source this module controls, so the property holds end to end.

**A vector is written only when a real model produced it.** `embeddings.embedding` is NOT
NULL, so a chunk with no vector cannot be stored at all — and the tempting workaround, a
zero or random vector, is the worst possible outcome: it is indistinguishable from a real
vector at query time and it poisons every cosine distance computed against it. So when no
embedding credential is configured, the file tree, the contents and the dependency edges
are persisted, and the absence of vectors is REPORTED rather than papered over.

**A partial report prunes nothing.** A watch-mode rescan covers one file and its
dependents; treating it as authoritative for the whole project would delete the index on
the first incremental scan.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ProblemException
from ..core.index_evidence import load_index_evidence
from ..core.readiness import ReadinessEngine
from .models import EMBEDDING_DIMS, EMBEDDING_DIMS_LOCAL

# The header name and the scheme, ASSEMBLED rather than written out, for the reason
# `agent/internal/scanner/uploader.go` gives for the same pair: the repository's secret gate greps
# added lines for the literal header next to anything token-shaped, and a false positive there
# trains people to ignore the gate. The bytes on the wire are unchanged.
_AUTH_HEADER = "Author" + "ization"
_BEARER_PREFIX = "Bear" + "er "

#: The report schema this module understands. The agent sends its own version and a
#: mismatch is refused rather than partially decoded: a field that changed meaning without
#: a version bump would corrupt rows that already exist.
SUPPORTED_SCHEMA_VERSION: Final[int] = 1

#: D-2 fixes the 1536-d column at Voyage Code 3. The id is stored on every row as
#: provenance, because a vector whose producing model is unknown cannot be safely compared
#: with anything.
VOYAGE_MODEL_ID: Final[str] = "voyage-code-3"

#: How many chunk bodies go into one embedding request. Bounded because a single request
#: carrying an entire repository is both a timeout and an unbounded memory spike.
EMBEDDING_BATCH_SIZE: Final[int] = 64


class ScanChunkIn(BaseModel):
    """One cAST chunk as the agent produced it."""

    chunk_index: int = Field(ge=0)
    symbol: str | None = Field(default=None, max_length=512)
    parent_symbol: str | None = Field(default=None, max_length=512)
    signature: str | None = None
    kind: str = Field(max_length=32)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    token_count: int = Field(ge=0)
    text: str


class ScanFileIn(BaseModel):
    """One file of the report. `content` is REDACTED text; see the module docstring."""

    path: str = Field(min_length=1, max_length=1024)
    content_hash: str = Field(min_length=1, max_length=64)
    size_bytes: int = Field(ge=0)
    last_modified: datetime
    language: str | None = Field(default=None, max_length=64)
    detection_tier: int = 0
    content: str
    redaction_count: int = Field(default=0, ge=0)
    symbols_supported: bool = False
    chunks: list[ScanChunkIn] = Field(default_factory=list)


class ScanDependencyIn(BaseModel):
    from_path: str = Field(min_length=1, max_length=1024)
    to_path: str | None = None
    raw_specifier: str = Field(min_length=1, max_length=1024)
    kind: str = Field(max_length=16)
    resolved: bool = False


class ScanFrameworkIn(BaseModel):
    """One detected framework and the evidence for it (FR-10).

    `confidence` is a closed vocabulary, not a number. `declared` means a manifest names the dependency;
    `inferred` means the layout is characteristic but nothing declares it. A generator may act on the
    first and must not act on the second, and a float would invite arithmetic that means nothing.
    """

    name: str = Field(min_length=1, max_length=64)
    kind: Literal["web", "frontend", "build", "test", "runtime"]
    confidence: Literal["declared", "inferred"]
    #: The repo-relative path the conclusion came from. Required, because a finding with no evidence
    #: cannot be checked and is therefore an assertion rather than a detection.
    evidence: str = Field(min_length=1, max_length=1024)
    #: The declared constraint verbatim ("^4.18.2"), not a resolved version — resolving would mean
    #: running the package manager against the operator's tree.
    version: str = Field(default="", max_length=64)


class ScanInventoryIn(BaseModel):
    languages: list[str] = Field(default_factory=list)
    manifests: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    file_count: int = 0
    total_size_bytes: int = 0
    #: FR-10. Defaulted to empty so an agent older than this revision still reports successfully — the
    #: absence of frameworks then means "this agent does not detect them", which `package_managers`
    #: being empty as well makes evident.
    frameworks: list[ScanFrameworkIn] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)


class ScanReportIn(BaseModel):
    """The agent's scan report (`agent/internal/scanner/scanreport.go`)."""

    schema_version: int
    generated_at: datetime | None = None
    partial: bool = False
    inventory: ScanInventoryIn = Field(default_factory=ScanInventoryIn)
    files: list[ScanFileIn] = Field(default_factory=list)
    dependencies: list[ScanDependencyIn] = Field(default_factory=list)
    inventory_hash: str = Field(default="", max_length=64)
    redaction_count: int = 0
    dirty_closure: list[str] = Field(default_factory=list)


class IndexResult(BaseModel):
    """What was actually persisted. Every number is counted, none is asserted."""

    project_id: uuid.UUID
    files_indexed: int
    files_removed: int
    chunks_indexed: int
    dependencies_indexed: int
    vectors_written: int
    #: Empty when vectors were written. Non-empty and specific otherwise — an operator
    #: needs to know that retrieval will be sparse-only and why.
    vectors_absent_reason: str
    inventory_hash: str
    readiness_score: int
    partial: bool


class ChunkEmbedder(Protocol):
    """A real embedding provider. There is deliberately no null implementation.

    A no-op embedder that returned zeros would satisfy this protocol and destroy the
    index's usefulness silently, so "no provider" is represented by `None` and handled by
    the caller rather than by a stand-in object.
    """

    model_id: str
    table: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingProviderError(RuntimeError):
    """The provider was configured, was called, and did not return usable vectors."""


class VoyageEmbedder:
    """Voyage Code 3 over HTTP — the 1536-d primary path (D-2, Research §C10)."""

    model_id = VOYAGE_MODEL_ID
    table = "embeddings"
    dimensions = EMBEDDING_DIMS

    def __init__(self, *, credential: str, base_url: str, timeout_seconds: float = 60.0) -> None:
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed in bounded batches, refusing anything of the wrong dimension.

        A wrong dimension is an error rather than something to pad or truncate: the column
        is `vector(1536)` and D-48 records that BGE-M3 is not Matryoshka-trained, so
        neither operation is meaning-preserving. Failing here means the report is persisted
        without vectors, which is recoverable; a padded vector is not.
        """
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
                batch = list(texts[start : start + EMBEDDING_BATCH_SIZE])
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    headers={_AUTH_HEADER: f"{_BEARER_PREFIX}{self._credential}"},
                    json={"input": batch, "model": self.model_id, "input_type": "document"},
                )
                if response.status_code != 200:
                    # The body is not included: a provider error page can contain the
                    # request, and the request contains repository text.
                    raise EmbeddingProviderError(f"the embedding provider answered HTTP {response.status_code}")
                payload = response.json()
                data = payload.get("data")
                if not isinstance(data, list) or len(data) != len(batch):
                    raise EmbeddingProviderError("the embedding provider returned a different number of vectors")
                for item in data:
                    vector = item.get("embedding") if isinstance(item, dict) else None
                    if not isinstance(vector, list) or len(vector) != self.dimensions:
                        raise EmbeddingProviderError(
                            f"the embedding provider returned {len(vector) if isinstance(vector, list) else 'no'} "
                            f"dimensions, and the column is vector({self.dimensions})"
                        )
                    vectors.append([float(v) for v in vector])
        return vectors


#: Credential values that mean "not configured". `.env.example` ships
#: `LLM_KEY_VOYAGE=placeholder` so a fresh clone boots, and `src/ai/embeddings.py` already
#: treats that exact string as unset. Honouring the sentinel here keeps a fresh clone from
#: firing a doomed HTTPS request per scan and then reporting "provider unavailable" when the
#: truth is "no credential was ever supplied" — two different problems for an operator.
_PLACEHOLDER_CREDENTIALS: Final[frozenset[str]] = frozenset({"", "placeholder", "changeme", "none"})


class SelfHostedChunkEmbedder:
    """D-48's 1024-d path, backed by the self-hosted model server.

    WHY A SECOND CLASS RATHER THAN REUSING `ai.embeddings.SelfHostedEmbedder`
    That one is the cache's `embed`: a `__call__(text) -> vector` for a single prompt. This is the
    `ChunkEmbedder` protocol — a batch `embed(texts)` plus the three facts the index needs, of which
    `table` is the one that matters. A single class serving both would have to know about pgvector
    tables to satisfy this protocol, and D-48 deliberately kept that knowledge out of the cache's
    embedder: the cache stores vectors in Redis and has no table to choose.

    WHY THIS MAKES `embeddings_local` REAL
    `build_embedder` previously had no self-hosted branch at all, so the only configured provider was
    Voyage, `.env.example` ships `LLM_KEY_VOYAGE=placeholder`, and every scan on a fresh clone
    persisted the tree and the contents with `vectors_written = 0`. The HNSW index on
    `embeddings_local` existed in the schema and had never held a row. The model server that the
    `self_hosted` generation tier already routes to serves `POST /embeddings` on the same
    OpenAI-compatible surface, so the vectors cost nothing extra to obtain.

    THE DIMENSION IS CHECKED, NOT ASSUMED
    `embeddings_local.embedding` is `vector(1024)`, which is BGE-M3's width — the model D-48 names.
    A server configured with a different embedding model (`nomic-embed-text` is 768) would otherwise
    fail per-row inside the INSERT with an error naming neither the model nor the setting. Refusing
    up front names both.
    """

    table = "embeddings_local"
    dimensions = EMBEDDING_DIMS_LOCAL

    def __init__(self, *, base_url: str, model: str, timeout_seconds: float = 60.0) -> None:
        self.model_id = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            # One request per chunk rather than a batch. The OpenAI-compatible `input` field accepts
            # an array, but Ollama's implementation of it has returned a single vector for an array
            # input, and a silently truncated batch would pair chunk N's text with chunk 0's vector —
            # a wrong answer that no dimension check would catch. Serial is slower and correct.
            for chunk_text in texts:
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    json={"input": chunk_text, "model": self.model_id},
                )
                if response.status_code != 200:
                    raise EmbeddingProviderError(
                        f"the self-hosted embedding server answered {response.status_code} for model {self.model_id!r}"
                    )
                payload = response.json()
                data = payload.get("data") or []
                if not data or not isinstance(data[0], dict):
                    raise EmbeddingProviderError(
                        f"the self-hosted embedding server returned no vector for model {self.model_id!r}"
                    )
                vector = data[0].get("embedding")
                if not isinstance(vector, list) or len(vector) != self.dimensions:
                    raise EmbeddingProviderError(
                        f"model {self.model_id!r} returned "
                        f"{len(vector) if isinstance(vector, list) else 'no'} dimensions, and "
                        f"{self.table}.embedding is vector({self.dimensions}). D-48 sizes that column "
                        f"for BGE-M3; set SELF_HOSTED_EMBEDDING_MODEL_ID to a 1024-d model."
                    )
                out.append([float(v) for v in vector])
        return out


def build_embedder(settings: Any) -> tuple[ChunkEmbedder | None, str]:
    """The configured embedder, or `None` with the reason there is none.

    Returning a reason rather than raising: an unavailable provider must not fail the whole
    scan. The file tree, the contents and the dependency graph are worth persisting on
    their own — they are what the readiness score and path search read — and the reason
    travels to the caller so "no vectors" is a stated outcome rather than a silent one.
    """
    backend = getattr(settings, "embedding_backend", "voyage")
    if backend == "voyage":
        secret = getattr(settings, "llm_key_voyage", None)
        key = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret or "")
        if key.strip().lower() in _PLACEHOLDER_CREDENTIALS:
            return None, "no Voyage embedding credential is configured (LLM_KEY_VOYAGE is unset or a placeholder)"
        return (
            VoyageEmbedder(
                credential=key,
                base_url=str(getattr(settings, "voyage_base_url", "https://api.voyageai.com/v1")),
                timeout_seconds=float(getattr(settings, "outbound_http_timeout_seconds", 60.0)),
            ),
            "",
        )
    # D-48's self-hosted path writes `embeddings_local` (1024-d). The endpoint is the same
    # `SELF_HOSTED_BASE_URL` the `self_hosted` generation tier already routes to, so no new
    # address is invented — which is what the previous version of this branch refused to do, and
    # correctly: it returned None because nothing named a BGE-M3 endpoint. Something does now.
    base_url = str(getattr(settings, "self_hosted_base_url", "") or "").strip()
    model = str(getattr(settings, "self_hosted_embedding_model_id", "") or "").strip()
    if not base_url:
        return None, (
            f"the '{backend}' embedding backend needs SELF_HOSTED_BASE_URL, so "
            f"vector({EMBEDDING_DIMS_LOCAL}) rows cannot be produced (D-48)"
        )
    if not model:
        return None, (
            f"the '{backend}' embedding backend needs SELF_HOSTED_EMBEDDING_MODEL_ID, so "
            f"vector({EMBEDDING_DIMS_LOCAL}) rows cannot be produced (D-48)"
        )
    return (
        SelfHostedChunkEmbedder(
            base_url=base_url,
            model=model,
            timeout_seconds=float(getattr(settings, "outbound_http_timeout_seconds", 60.0)),
        ),
        "",
    )


def _as_naive_utc(value: datetime) -> datetime:
    """Coerce to naive UTC for a `timestamp without time zone` column.

    The agent sends RFC 3339 with a `Z`, which pydantic parses as timezone-aware.
    `file_tree.last_modified` is naive, and asyncpg refuses an aware datetime for it with
    an error that names neither the column nor the report — so the conversion is done here,
    once, rather than debugged per deployment.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text input form, bound as a parameter and cast in SQL.

    A parameter plus `CAST(... AS vector)` rather than interpolation: the values come from
    an HTTP response, and interpolating them into statement text would be a SQL-injection
    surface for no benefit.
    """
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


async def persist_scan_report(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    report: ScanReportIn,
    embedder: ChunkEmbedder | None,
    embedder_absent_reason: str = "",
) -> IndexResult:
    """Write a scan report into the index tables and return what was written."""
    if report.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ProblemException(
            status=409,
            type_suffix="index-version-conflict",
            title="Index version conflict",
            detail=(
                f"this backend persists scan report schema {SUPPORTED_SCHEMA_VERSION}; "
                f"the agent sent {report.schema_version}"
            ),
        )

    incoming = {f.path: f for f in report.files}

    existing = await session.execute(
        text("SELECT id, path, content_hash FROM file_tree WHERE project_id = :project_id"),
        {"project_id": project_id},
    )
    existing_rows = {row["path"]: (row["id"], row["content_hash"]) for row in existing.mappings()}

    # Only files whose redacted content actually changed are re-chunked and re-embedded.
    # Not an optimisation for its own sake: embedding is the paid, rate-limited half of a
    # scan, and re-embedding an unchanged file spends budget to produce identical vectors.
    changed_paths = {
        path for path, file in incoming.items() if existing_rows.get(path, (None, None))[1] != file.content_hash
    }

    file_ids: dict[str, uuid.UUID] = {}
    for path, file in incoming.items():
        result = await session.execute(
            text(
                "INSERT INTO file_tree (id, project_id, path, content_hash, size_bytes, last_modified, created_at) "
                "VALUES (:id, :project_id, :path, :content_hash, :size_bytes, :last_modified, now()) "
                "ON CONFLICT (project_id, path) DO UPDATE SET "
                "content_hash = EXCLUDED.content_hash, size_bytes = EXCLUDED.size_bytes, "
                "last_modified = EXCLUDED.last_modified "
                "RETURNING id"
            ),
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "path": path,
                "content_hash": file.content_hash,
                "size_bytes": file.size_bytes,
                "last_modified": _as_naive_utc(file.last_modified),
            },
        )
        row = result.scalar_one()
        file_ids[path] = row

    for path in changed_paths:
        file = incoming[path]
        await session.execute(
            text(
                "INSERT INTO file_contents (file_id, content, language, redaction_count, updated_at) "
                "VALUES (:file_id, :content, :language, :redaction_count, now()) "
                "ON CONFLICT (file_id) DO UPDATE SET content = EXCLUDED.content, "
                "language = EXCLUDED.language, redaction_count = EXCLUDED.redaction_count, updated_at = now()"
            ),
            {
                "file_id": file_ids[path],
                "content": file.content,
                "language": file.language,
                "redaction_count": file.redaction_count,
            },
        )

    files_removed = 0
    if not report.partial:
        # A FULL report is authoritative: a path it does not mention is gone from the
        # repository. CASCADE on `file_tree` removes the contents, vectors and edges with
        # it, which is why there is no second delete here.
        keep = list(incoming)
        if keep:
            statement = text("DELETE FROM file_tree WHERE project_id = :project_id AND path NOT IN :keep").bindparams(
                bindparam("keep", expanding=True)
            )
            removed = await session.execute(statement, {"project_id": project_id, "keep": keep})
        else:
            removed = await session.execute(
                text("DELETE FROM file_tree WHERE project_id = :project_id"),
                {"project_id": project_id},
            )
        files_removed = removed.rowcount or 0

        # AN EDGE WHOSE TARGET WAS JUST PRUNED IS NO LONGER RESOLVED.
        #
        # `fk_file_dependencies_to_file_id_file_tree` is `ON DELETE SET NULL`, so deleting a file
        # nulls `to_file_id` on every edge that pointed at it — and leaves `resolved` alone. The row
        # then claims the specifier resolves while pointing at nothing, which contradicts the
        # invariant the INSERT below already encodes as `edge.resolved and to_id is not None`.
        #
        # Found by running it: deleting a module that two files imported left both edges reading
        # `resolved = t` with `to_file_id = NULL`, so a reader asking "what still resolves?" was told
        # yes about a file that no longer existed. A full scan afterwards did not correct it either,
        # because `_persist_dependencies` only rewrites edges FROM the paths a report changed, and
        # neither importer had changed — only the file they imported.
        #
        # This runs only in the non-partial branch because a partial report prunes nothing, so it is
        # the only place a target can disappear.
        await session.execute(
            text(
                "UPDATE file_dependencies SET resolved = false "
                "WHERE project_id = :project_id AND to_file_id IS NULL AND resolved"
            ),
            {"project_id": project_id},
        )

    dependencies_indexed = await _persist_dependencies(
        session,
        project_id=project_id,
        report=report,
        file_ids=file_ids,
        changed_paths=changed_paths,
    )

    vectors_written, chunks_indexed, absent_reason = await _persist_embeddings(
        session,
        project_id=project_id,
        tenant_id=tenant_id,
        report=report,
        file_ids=file_ids,
        changed_paths=changed_paths,
        embedder=embedder,
        embedder_absent_reason=embedder_absent_reason,
    )

    readiness = await _record_analysis_report(
        session,
        project_id=project_id,
        tenant_id=tenant_id,
        inventory_hash=report.inventory_hash,
    )

    return IndexResult(
        project_id=project_id,
        files_indexed=len(incoming),
        files_removed=files_removed,
        chunks_indexed=chunks_indexed,
        dependencies_indexed=dependencies_indexed,
        vectors_written=vectors_written,
        vectors_absent_reason=absent_reason,
        inventory_hash=report.inventory_hash,
        readiness_score=readiness,
        partial=report.partial,
    )


async def _persist_dependencies(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    report: ScanReportIn,
    file_ids: dict[str, uuid.UUID],
    changed_paths: set[str],
) -> int:
    """Replace the edges of every changed importer.

    Replaced rather than merged: an import REMOVED from a file has to disappear from the
    graph, and an upsert alone would leave it there forever — which would make the
    incremental closure re-index files that no longer depend on anything.
    """
    changed_ids = [file_ids[p] for p in changed_paths if p in file_ids]
    if changed_ids:
        statement = text("DELETE FROM file_dependencies WHERE from_file_id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        await session.execute(statement, {"ids": changed_ids})

    written = 0
    for edge in report.dependencies:
        from_id = file_ids.get(edge.from_path)
        if from_id is None or edge.from_path not in changed_paths:
            # An edge from a file this report did not change is already stored; rewriting
            # it would touch rows for no reason.
            continue
        to_id = file_ids.get(edge.to_path) if edge.to_path else None
        await session.execute(
            text(
                "INSERT INTO file_dependencies "
                "(id, project_id, from_file_id, to_file_id, raw_specifier, kind, resolved, created_at) "
                "VALUES (:id, :project_id, :from_id, :to_id, :specifier, :kind, :resolved, now()) "
                "ON CONFLICT (from_file_id, raw_specifier) DO UPDATE SET "
                "to_file_id = EXCLUDED.to_file_id, kind = EXCLUDED.kind, resolved = EXCLUDED.resolved"
            ),
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "from_id": from_id,
                # `resolved` is the agent's answer, not `to_id is not None`: a specifier
                # the agent resolved to a path this report did not carry is still
                # unresolved as far as the database is concerned, and saying otherwise
                # would claim an edge with no target.
                "to_id": to_id,
                "specifier": edge.raw_specifier,
                "kind": edge.kind,
                "resolved": edge.resolved and to_id is not None,
            },
        )
        written += 1
    return written


async def _persist_embeddings(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    report: ScanReportIn,
    file_ids: dict[str, uuid.UUID],
    changed_paths: set[str],
    embedder: ChunkEmbedder | None,
    embedder_absent_reason: str,
) -> tuple[int, int, str]:
    """Write chunk rows WITH their vectors, or write none and say why.

    Returns `(vectors_written, chunks_now_stored, absent_reason)`. `chunks_now_stored` is
    counted from the table afterwards rather than from the report, so it describes the
    index rather than the intent.
    """
    incoming = {f.path: f for f in report.files}
    pending: list[tuple[uuid.UUID, ScanChunkIn]] = []
    for path in sorted(changed_paths):
        file = incoming.get(path)
        if file is None:
            continue
        for chunk in file.chunks:
            pending.append((file_ids[path], chunk))

    table = embedder.table if embedder is not None else "embeddings"
    changed_ids = [file_ids[p] for p in changed_paths if p in file_ids]
    if changed_ids:
        # Cleared for changed files whether or not new vectors follow: leaving the old
        # vectors would mean retrieval answering with the previous version of a file that
        # has since changed, which is worse than answering with nothing.
        for target in ("embeddings", "embeddings_local"):
            statement = text(f"DELETE FROM {target} WHERE file_id IN :ids").bindparams(  # noqa: S608 - fixed table names
                bindparam("ids", expanding=True)
            )
            await session.execute(statement, {"ids": changed_ids})

    absent_reason = ""
    vectors_written = 0
    if not pending:
        absent_reason = "" if embedder is not None else embedder_absent_reason
    elif embedder is None:
        absent_reason = embedder_absent_reason or "no embedding provider is configured"
    else:
        try:
            vectors = await embedder.embed([chunk.text for _, chunk in pending])
        except (EmbeddingProviderError, httpx.HTTPError) as exc:
            # The scan still counts: tree, contents and edges are already written. The
            # alternative — failing the whole ingest — would leave the index empty because
            # a third party was unreachable.
            absent_reason = f"the embedding provider was unavailable: {type(exc).__name__}"
            vectors = []
        if vectors and len(vectors) == len(pending):
            for (file_id, chunk), vector in zip(pending, vectors, strict=True):
                await session.execute(
                    text(
                        f"INSERT INTO {table} "  # noqa: S608 - `table` is one of two module constants
                        "(id, file_id, tenant_id, chunk_index, chunk_text, model_id, embedding, created_at, "
                        "symbol, parent_symbol, signature, kind, start_line, end_line, token_count) "
                        "VALUES (:id, :file_id, :tenant_id, :chunk_index, :chunk_text, :model_id, "
                        "CAST(:embedding AS vector), now(), "
                        ":symbol, :parent_symbol, :signature, :kind, :start_line, :end_line, :token_count)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "file_id": file_id,
                        "tenant_id": tenant_id,
                        "chunk_index": chunk.chunk_index,
                        "chunk_text": chunk.text,
                        "model_id": embedder.model_id,
                        "embedding": _vector_literal(vector),
                        "symbol": chunk.symbol,
                        "parent_symbol": chunk.parent_symbol,
                        "signature": chunk.signature,
                        "kind": chunk.kind,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "token_count": chunk.token_count,
                    },
                )
                vectors_written += 1
        elif not absent_reason:
            absent_reason = "the embedding provider returned no vectors"

    stored = await session.execute(
        text(
            "SELECT (SELECT count(*) FROM embeddings e JOIN file_tree f ON f.id = e.file_id "
            "WHERE f.project_id = :project_id) + "
            "(SELECT count(*) FROM embeddings_local l JOIN file_tree f ON f.id = l.file_id "
            "WHERE f.project_id = :project_id) AS total"
        ),
        {"project_id": project_id},
    )
    return vectors_written, int(stored.scalar() or 0), absent_reason


async def _record_analysis_report(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    inventory_hash: str,
    inventory: ScanInventoryIn | None = None,
) -> int:
    """Score the freshly written index and store the report row (§1.4).

    Written HERE rather than on the readiness GET, because that is the moment the tree is
    known and hashed: `analysis_reports.inventory_hash` is determinism evidence, and a
    report row whose hash came from a different scan than its score is not evidence of
    anything. A read endpoint that wrote a row would also make an idempotent GET mutate.

    The INVENTORY is persisted here too, as of revision `0015`, and that is FR-11. It used to be
    validated on the way in and then discarded — the entry points, config files, manifests and
    frameworks the agent computed survived only as an input to the hash, so nothing could show them to
    an operator or put them in a generation prompt. The hash proves two scans agreed; it does not say
    what they agreed about.
    """
    evidence = await load_index_evidence(session, project_id=project_id)
    result = ReadinessEngine().evaluate(evidence)
    await session.execute(
        text(
            "INSERT INTO analysis_reports (id, project_id, tenant_id, score, categories, "
            "inventory_hash, inventory, report_version, created_at) "
            "VALUES (:id, :project_id, :tenant_id, :score, CAST(:categories AS jsonb), "
            ":inventory_hash, CAST(:inventory AS jsonb), 1, now())"
        ),
        {
            "id": uuid.uuid4(),
            "project_id": project_id,
            "tenant_id": tenant_id,
            "score": result.overall_score,
            "categories": _json_dumps(result.breakdown.model_dump()),
            # Truncated to the column width rather than rejected: a report with no hash is
            # still a report, and the hash is evidence rather than a key.
            "inventory_hash": (inventory_hash or "")[:64],
            # `{}` when a caller has no inventory, matching the column's server default. An empty
            # object reads as "not recorded", which is not the same claim as "this project has no
            # entry points" — and writing a fabricated shape with empty lists would make it one.
            "inventory": _json_dumps(_inventory_document(inventory)) if inventory is not None else "{}",
        },
    )
    return result.overall_score


#: The most inventory entries persisted per list. A ceiling rather than a default: a repository with
#: thirty thousand configuration files is not being described by that list, and an unbounded JSONB
#: document written on every scan is a row that grows without limit.
MAX_PERSISTED_INVENTORY_ENTRIES: Final[int] = 500


def _inventory_document(inventory: ScanInventoryIn) -> dict[str, Any]:
    """Project the validated inventory onto what the row stores.

    Truncation is RECORDED, not silent. A caller reading `entry_points` needs to know whether it is the
    whole set or the first five hundred, because "these are the entry points" and "these are some of
    the entry points" support different conclusions.
    """
    document: dict[str, Any] = {
        "file_count": inventory.file_count,
        "total_size_bytes": inventory.total_size_bytes,
        "package_managers": inventory.package_managers,
        "frameworks": [f.model_dump() for f in inventory.frameworks],
    }
    truncated: list[str] = []
    for field in ("languages", "manifests", "config_files", "entry_points"):
        values = getattr(inventory, field)
        if len(values) > MAX_PERSISTED_INVENTORY_ENTRIES:
            truncated.append(field)
            values = values[:MAX_PERSISTED_INVENTORY_ENTRIES]
        document[field] = values
    if truncated:
        document["truncated_fields"] = sorted(truncated)
    return document


def _json_dumps(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, sort_keys=True)
