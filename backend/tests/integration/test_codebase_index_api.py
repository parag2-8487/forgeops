# SPDX-License-Identifier: FSL-1.1-ALv2
"""The Codebase Index API against a REAL PostgreSQL (design §7.6, phases.md §1.3).

**Rewritten, and the previous version is the reason.** It asserted
`data["status"] == "ready"`, `data[0]["name"] == "NewParser"` and
`data["chunk_id"] == "chunk-123"` against handlers that returned literals — so it passed
against an empty database and would have kept passing if the index tables had been dropped.
A test that cannot fail when the feature is absent is worse than no test: it reports the
feature as present.

Every case here writes rows through the ingest endpoint and then reads them back, so the
only way to pass is for the index to exist. The one exception is the two cases that need
vectors: `embeddings.embedding` is NOT NULL and no embedding credential is configured in
test, so those rows are inserted directly with a fixture vector — a fixture in a test,
never on a runtime path.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from src.auth.dependencies import require_principal
from src.auth.device_dependencies import require_device
from src.auth.models import UserRole
from src.auth.principal import Principal

from tests.integration.production_app import apply_committed_baseline_env

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

TENANT = uuid.UUID("6d1b7a90-5c31-4d2e-9f77-2a3b4c5d6e71")
OTHER_TENANT = uuid.UUID("7e2c8ba1-6d42-4e3f-8a88-3b4c5d6e7f82")
USER = uuid.UUID("8f3d9cb2-7e53-4f40-9b99-4c5d6e7f8a93")


class _StubDevice:
    """What `authenticate_session` returns, reduced to the two fields the route reads."""

    def __init__(self, project_id: uuid.UUID, tenant_id: uuid.UUID | None) -> None:
        self.project_id = project_id
        self.tenant_id = tenant_id


def _device(request: Request) -> _StubDevice:
    """A device paired to WHICHEVER project the request names.

    Reads the path parameter rather than pinning an id, because these tests create their projects
    dynamically. That deliberately makes the project-scoping check a no-op HERE -- it is asserted in
    `test_index_route_device_auth.py`, together with the two-factor refusals, which is where a
    weaker credential is proved insufficient. `_device_pinned_to` below is how a test in this file
    exercises the mismatch.
    """
    return _StubDevice(uuid.UUID(str(request.path_params["project_id"])), TENANT)


def _device_pinned_to(project_id: uuid.UUID, tenant_id: uuid.UUID | None = TENANT):
    """A device paired to one specific project, for the cross-boundary refusal."""

    def _dep(request: Request) -> _StubDevice:
        return _StubDevice(project_id, tenant_id)

    return _dep


#: The 1536-d fixture vector. A constant so the assertions are about the query path rather
#: than about a number, and 1536 because D-2 fixes `embeddings.embedding` at that width —
#: a shorter list is rejected by the column, which is the point of the dimension.
FIXTURE_VECTOR = "[" + ",".join(["0.01"] * 1536) + "]"


def _principal(tenant_id: uuid.UUID = TENANT) -> Principal:
    return Principal.for_user(
        user_id=USER,
        subject="index-test",
        email="indexer@example.invalid",
        role=UserRole.ADMIN,
        tenant_id=tenant_id,
    )


@pytest_asyncio.fixture
async def analysis_app(monkeypatch: pytest.MonkeyPatch, schema_at_head: str) -> AsyncIterator[Any]:
    """The real app against the migrated database.

    `apply_committed_baseline_env` points DATABASE_URL at a closed port on purpose; these
    handlers open a session, so it is overridden AFTER the baseline — the hook that file
    documents for "a test that needs real data".
    """
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", schema_at_head)
    redis_url = os.environ.get("FORGEOPS_TEST_REDIS_URL", "").strip()
    if redis_url:
        monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app()
    app.dependency_overrides[require_principal] = _principal
    # The index route authenticates a DEVICE, not a user: an agent holds a device token plus a
    # client certificate and can never satisfy `require_principal` (see
    # `auth/device_dependencies.py`). These tests are about what the endpoint PERSISTS, so the
    # authentication is overridden the same way the principal is — the two-factor check itself is
    # asserted in `test_index_route_device_auth.py`, which is where a weaker credential is proved
    # to be refused.
    app.dependency_overrides[require_device] = _device
    async with LifespanManager(app):
        yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(analysis_app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=analysis_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _create_project(client: AsyncClient, name: str, settings: dict[str, Any] | None = None) -> uuid.UUID:
    response = await client.post(
        "/api/v1/projects",
        json={
            "name": name,
            "path": "/srv/projects/indexed",
            "repo_url": "https://github.com/parag8487/ForgeOps",
            "settings": settings or {},
        },
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def _file(
    path: str,
    content: str,
    *,
    language: str,
    content_hash: str | None = None,
    chunks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "path": path,
        # Derived from the content so a changed body means a changed hash, exactly as the
        # agent computes it over the redacted text.
        "content_hash": content_hash or f"{abs(hash((path, content))):064x}"[:64],
        "size_bytes": len(content.encode("utf-8")),
        "last_modified": datetime(2026, 8, 26, 12, 0, tzinfo=UTC).isoformat(),
        "language": language,
        "detection_tier": 2,
        "content": content,
        "redaction_count": 0,
        "symbols_supported": True,
        "chunks": chunks or [],
    }


DOCKERFILE = "FROM golang:1.24 AS build\nRUN go build ./...\n\nFROM gcr.io/distroless/static\nUSER 65532:65532\n"


def _report(files: list[dict[str, Any]], *, partial: bool = False, dependencies: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "generated_at": datetime(2026, 8, 26, 12, 0, tzinfo=UTC).isoformat(),
        "partial": partial,
        "inventory": {
            "languages": sorted({f["language"] for f in files if f["language"]}),
            "manifests": [],
            "config_files": [],
            "entry_points": [],
            "file_count": len(files),
            "total_size_bytes": sum(f["size_bytes"] for f in files),
        },
        "files": files,
        "dependencies": dependencies or [],
        "inventory_hash": "b" * 64,
        "redaction_count": 0,
    }


# ─── the honest empty answers ────────────────────────────────────────────────


async def test_status_reports_an_empty_index_as_empty(client: AsyncClient) -> None:
    """It used to answer `indexed_files=42, total_chunks=128, status="ready"` — for a
    database in which `file_tree` had never held a row."""
    project_id = await _create_project(client, "Unscanned")

    response = await client.get(f"/api/v1/analysis/codebase/{project_id}/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["indexed_files"] == 0
    assert body["total_chunks"] == 0
    assert body["languages"] == []
    assert body["status"] == "empty"
    assert body["last_indexed_at"] is None


async def test_symbols_are_empty_for_an_unindexed_project(client: AsyncClient) -> None:
    """It used to return a fixed `NewParser` at `ast.go:35` for any query at all."""
    project_id = await _create_project(client, "No symbols")

    response = await client.get(f"/api/v1/analysis/codebase/{project_id}/symbols?query=NewParser")
    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_an_unknown_chunk_is_refused_rather_than_fabricated(client: AsyncClient) -> None:
    """It used to answer 200 with `content="func NewParser() ..."` for any chunk id."""
    project_id = await _create_project(client, "No chunks")

    response = await client.get(f"/api/v1/analysis/codebase/{project_id}/chunks/{uuid.uuid4()}")
    # The non-disclosing 403: a chunk that does not exist and a chunk belonging to another
    # project must answer identically, or the difference enumerates ids (§4.2, Q-20).
    assert response.status_code == 403, response.text


async def test_another_tenant_cannot_read_the_index(analysis_app: Any, client: AsyncClient) -> None:
    project_id = await _create_project(client, "Private index")

    analysis_app.dependency_overrides[require_principal] = lambda: _principal(OTHER_TENANT)
    for path in ("status", "symbols", f"chunks/{uuid.uuid4()}"):
        response = await client.get(f"/api/v1/analysis/codebase/{project_id}/{path}")
        assert response.status_code == 403, (path, response.text)


# ─── ingest ──────────────────────────────────────────────────────────────────


async def test_a_scan_report_populates_the_index(client: AsyncClient) -> None:
    project_id = await _create_project(client, "Scanned")
    report = _report(
        [
            _file("Dockerfile", DOCKERFILE, language="dockerfile"),
            _file("main.go", "package main\n\nfunc main() {}\n", language="go"),
        ],
        dependencies=[
            {
                "from_path": "main.go",
                "to_path": None,
                "raw_specifier": "fmt",
                "kind": "import",
                "resolved": False,
            }
        ],
    )

    posted = await client.post(f"/api/v1/analysis/codebase/{project_id}/index", json=report)
    assert posted.status_code == 200, posted.text
    result = posted.json()
    assert result["files_indexed"] == 2
    assert result["dependencies_indexed"] == 1
    assert result["files_removed"] == 0

    status = (await client.get(f"/api/v1/analysis/codebase/{project_id}/status")).json()
    assert status["indexed_files"] == 2
    assert sorted(status["languages"]) == ["dockerfile", "go"]
    assert status["total_bytes"] > 0
    assert status["unresolved_dependencies"] == 1
    assert status["resolved_dependencies"] == 0
    assert status["last_indexed_at"] is not None


async def test_no_vectors_are_written_when_no_provider_is_configured(client: AsyncClient) -> None:
    """The rule this pins: never a zero or random vector.

    A fabricated vector is indistinguishable from a real one at query time and poisons every
    cosine distance computed against it, so the honest outcome is tree plus contents plus
    edges, zero vectors, and a stated reason.
    """
    project_id = await _create_project(client, "No provider")
    report = _report(
        [
            _file(
                "main.go",
                "package main\n\nfunc main() {}\n",
                language="go",
                chunks=[
                    {
                        "chunk_index": 0,
                        "symbol": "main",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 3,
                        "token_count": 4,
                        "text": "func main() {}",
                    }
                ],
            )
        ]
    )

    result = (await client.post(f"/api/v1/analysis/codebase/{project_id}/index", json=report)).json()
    assert result["vectors_written"] == 0
    assert result["vectors_absent_reason"] != ""
    assert result["chunks_indexed"] == 0

    status = (await client.get(f"/api/v1/analysis/codebase/{project_id}/status")).json()
    # Tree and contents present, vectors absent — and the status says exactly that rather
    # than "ready".
    assert status["indexed_files"] == 1
    assert status["total_chunks"] == 0
    assert status["status"] == "indexed_without_vectors"


async def test_a_full_report_prunes_a_file_that_left_the_repository(client: AsyncClient) -> None:
    project_id = await _create_project(client, "Pruned")
    first = _report(
        [
            _file("keep.go", "package a\n", language="go"),
            _file("gone.go", "package b\n", language="go"),
        ]
    )
    await client.post(f"/api/v1/analysis/codebase/{project_id}/index", json=first)

    second = _report([_file("keep.go", "package a\n", language="go")])
    result = (await client.post(f"/api/v1/analysis/codebase/{project_id}/index", json=second)).json()
    assert result["files_removed"] == 1

    status = (await client.get(f"/api/v1/analysis/codebase/{project_id}/status")).json()
    assert status["indexed_files"] == 1


async def test_a_partial_report_prunes_nothing(client: AsyncClient) -> None:
    """A watch-mode rescan covers one file; treating it as authoritative would delete the
    index on the first incremental scan."""
    project_id = await _create_project(client, "Incremental")
    await client.post(
        f"/api/v1/analysis/codebase/{project_id}/index",
        json=_report(
            [
                _file("keep.go", "package a\n", language="go"),
                _file("other.go", "package b\n", language="go"),
            ]
        ),
    )

    partial = _report([_file("keep.go", "package a\n// changed\n", language="go")], partial=True)
    result = (await client.post(f"/api/v1/analysis/codebase/{project_id}/index", json=partial)).json()
    assert result["files_removed"] == 0
    assert result["partial"] is True

    status = (await client.get(f"/api/v1/analysis/codebase/{project_id}/status")).json()
    assert status["indexed_files"] == 2


async def test_an_unknown_report_schema_is_refused(client: AsyncClient) -> None:
    """A field that changed meaning without a version bump would corrupt existing rows, so
    a partial decode is refused outright."""
    project_id = await _create_project(client, "Future agent")
    report = _report([_file("main.go", "package main\n", language="go")])
    report["schema_version"] = 99

    response = await client.post(f"/api/v1/analysis/codebase/{project_id}/index", json=report)
    assert response.status_code == 409, response.text
    assert "index-version-conflict" in response.text


async def test_ingest_into_another_tenants_project_is_refused(analysis_app: Any, client: AsyncClient) -> None:
    """A device may only index the project it is paired to.

    Expressed as a PROJECT mismatch rather than a tenant one, because that is what the mechanism
    now is: an agent authenticates as a device, and `agent_devices` pairs each device to exactly
    one project. The refusal is the same non-disclosing 403, so it is not an oracle for ids.
    """
    project_id = await _create_project(client, "Not yours")
    analysis_app.dependency_overrides[require_device] = _device_pinned_to(
        uuid.UUID("11111111-2222-3333-4444-555555555555"), OTHER_TENANT
    )

    response = await client.post(
        f"/api/v1/analysis/codebase/{project_id}/index",
        json=_report([_file("main.go", "package main\n", language="go")]),
    )
    assert response.status_code == 403, response.text


# ─── symbol and chunk reads over stored rows ─────────────────────────────────


async def _insert_chunk_row(database_url: str, *, project_id: uuid.UUID, path: str) -> uuid.UUID:
    """Insert one `embeddings` row for an already-indexed file, with a fixture vector.

    Direct SQL because `embeddings.embedding` is NOT NULL and the test environment has no
    embedding credential — so the only way to exercise the symbol and chunk QUERIES is to
    supply the vector here. The runtime path still refuses to invent one.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            file_id = await connection.scalar(
                text("SELECT id FROM file_tree WHERE project_id = :p AND path = :path"),
                {"p": project_id, "path": path},
            )
            assert file_id is not None, f"{path} was not indexed"
            chunk_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO embeddings (id, file_id, chunk_index, chunk_text, model_id, embedding, "
                    "created_at, symbol, parent_symbol, signature, kind, start_line, end_line, token_count) "
                    "VALUES (:id, :file_id, 0, :chunk_text, 'voyage-code-3', CAST(:vector AS vector), now(), "
                    "'NewParser', 'Parser', 'func NewParser(logger *zap.Logger) (*Parser, error)', "
                    "'function', 35, 60, 120)"
                ),
                {
                    "id": chunk_id,
                    "file_id": file_id,
                    "chunk_text": "func NewParser(logger *zap.Logger) (*Parser, error) {\n\treturn nil, nil\n}",
                    "vector": FIXTURE_VECTOR,
                },
            )
            return chunk_id
    finally:
        await engine.dispose()


async def test_symbols_and_chunk_details_come_from_stored_rows(client: AsyncClient, schema_at_head: str) -> None:
    project_id = await _create_project(client, "With symbols")
    await client.post(
        f"/api/v1/analysis/codebase/{project_id}/index",
        json=_report([_file("agent/internal/scanner/ast/ast.go", "package ast\n", language="go")]),
    )
    chunk_id = await _insert_chunk_row(schema_at_head, project_id=project_id, path="agent/internal/scanner/ast/ast.go")

    symbols = await client.get(f"/api/v1/analysis/codebase/{project_id}/symbols?query=newpar")
    assert symbols.status_code == 200, symbols.text
    found = symbols.json()
    assert len(found) == 1
    assert found[0]["name"] == "NewParser"
    assert found[0]["kind"] == "function"
    assert found[0]["file_path"] == "agent/internal/scanner/ast/ast.go"
    assert found[0]["line_number"] == 35
    assert found[0]["parent_symbol"] == "Parser"
    assert found[0]["chunk_id"] == str(chunk_id)

    # A query that matches nothing returns nothing, rather than the one row that exists.
    empty = await client.get(f"/api/v1/analysis/codebase/{project_id}/symbols?query=zzz-no-such-symbol")
    assert empty.json() == []

    detail = await client.get(f"/api/v1/analysis/codebase/{project_id}/chunks/{chunk_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["chunk_id"] == str(chunk_id)
    assert body["file_path"] == "agent/internal/scanner/ast/ast.go"
    assert "func NewParser(" in body["content"]
    assert body["start_line"] == 35
    assert body["end_line"] == 60
    assert body["language"] == "go"
    assert body["model_id"] == "voyage-code-3"

    status = (await client.get(f"/api/v1/analysis/codebase/{project_id}/status")).json()
    assert status["total_chunks"] == 1
    assert status["status"] == "indexed"


async def test_a_chunk_of_another_project_is_not_readable_through_this_one(
    client: AsyncClient, schema_at_head: str
) -> None:
    """The scoping assertion. Without the `project_id` predicate on the join, one project's
    chunk id would read another project's source."""
    owner = await _create_project(client, "Owner")
    bystander = await _create_project(client, "Bystander")
    await client.post(
        f"/api/v1/analysis/codebase/{owner}/index",
        json=_report([_file("secret/service.go", "package service\n", language="go")]),
    )
    chunk_id = await _insert_chunk_row(schema_at_head, project_id=owner, path="secret/service.go")

    response = await client.get(f"/api/v1/analysis/codebase/{bystander}/chunks/{chunk_id}")
    assert response.status_code == 403, response.text


# ─── the vector write path ───────────────────────────────────────────────────
#
# `persist_scan_report` is called directly here because the decision under test is what it
# does with a provider, and the test environment deliberately has no embedding credential.
# The two embedders below are real implementations of the `ChunkEmbedder` protocol — not
# mocks — living in the test tree: one that answers, and one that fails the way an
# unreachable provider fails.


class _ConstantEmbedder:
    """Answers with a fixed 1536-d vector, so the assertion is about persistence."""

    model_id = "test-constant-embedder"
    table = "embeddings"
    dimensions = 1536

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[0.02] * self.dimensions for _ in texts]


class _UnavailableEmbedder:
    """Fails the way a provider outage fails."""

    model_id = "test-unavailable-embedder"
    table = "embeddings"
    dimensions = 1536

    async def embed(self, texts: Any) -> list[list[float]]:
        from src.analysis.indexer import EmbeddingProviderError

        raise EmbeddingProviderError("the provider is unreachable")


async def _persist_with(database_url: str, embedder: Any, *, project_name: str) -> tuple[Any, uuid.UUID]:
    from sqlalchemy.ext.asyncio import AsyncSession
    from src.analysis.indexer import ScanReportIn, persist_scan_report

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            project_id = uuid.uuid4()
            await connection.execute(
                text("INSERT INTO projects (id, tenant_id, name, path) VALUES (:id, :t, :name, '/srv/p')"),
                {"id": project_id, "t": TENANT, "name": f"{project_name}-{project_id.hex[:8]}"},
            )
            report = ScanReportIn.model_validate(
                _report(
                    [
                        _file(
                            "internal/repo/repo.go",
                            "package repo\n\nfunc New() {}\n",
                            language="go",
                            chunks=[
                                {
                                    "chunk_index": 0,
                                    "symbol": "New",
                                    "kind": "function",
                                    "start_line": 3,
                                    "end_line": 3,
                                    "token_count": 3,
                                    "text": "func New() {}",
                                }
                            ],
                        )
                    ]
                )
            )
            session = AsyncSession(bind=connection)
            result = await persist_scan_report(
                session,
                project_id=project_id,
                tenant_id=TENANT,
                report=report,
                embedder=embedder,
                embedder_absent_reason="",
            )
            stored = await connection.execute(
                text(
                    "SELECT e.model_id, e.symbol, e.kind, e.start_line, e.tenant_id, "
                    "vector_dims(e.embedding) AS dims FROM embeddings e "
                    "JOIN file_tree f ON f.id = e.file_id WHERE f.project_id = :p"
                ),
                {"p": project_id},
            )
            return (result, [dict(r) for r in stored.mappings()]), project_id
    finally:
        await engine.dispose()


async def test_a_configured_provider_writes_vectors_with_their_provenance(schema_at_head: str) -> None:
    (result, rows), _ = await _persist_with(schema_at_head, _ConstantEmbedder(), project_name="embedded")

    assert result.vectors_written == 1
    assert result.chunks_indexed == 1
    assert result.vectors_absent_reason == ""
    assert len(rows) == 1
    # D-2's provenance rule: a vector whose producing model is unknown cannot be safely
    # compared with anything, so `model_id` is stored on the row rather than inferred.
    assert rows[0]["model_id"] == "test-constant-embedder"
    assert rows[0]["dims"] == 1536
    # The cAST metadata revision 0003 added, which is what `/codebase/symbols` reads.
    assert rows[0]["symbol"] == "New"
    assert rows[0]["kind"] == "function"
    assert rows[0]["start_line"] == 3
    assert rows[0]["tenant_id"] == TENANT


async def test_an_unavailable_provider_leaves_the_tree_indexed_and_the_vectors_absent(
    schema_at_head: str,
) -> None:
    """The rule: never a zero or random vector, and never fail the whole scan either.

    The tree, the contents and the edges are worth persisting on their own — they are what
    the readiness score and path search read — so a provider outage costs the vectors and
    nothing else.
    """
    (result, rows), _ = await _persist_with(schema_at_head, _UnavailableEmbedder(), project_name="degraded")

    assert result.files_indexed == 1
    assert result.vectors_written == 0
    assert rows == []
    assert "unavailable" in result.vectors_absent_reason
    assert "EmbeddingProviderError" in result.vectors_absent_reason


def test_a_placeholder_credential_counts_as_no_credential() -> None:
    """`.env.example` ships `LLM_KEY_VOYAGE=placeholder` so a fresh clone boots.

    Treating that as configured would fire a doomed HTTPS request per scan and then report
    "provider unavailable", when the truth is that no credential was ever supplied.
    """
    from pydantic import SecretStr
    from src.analysis.indexer import VoyageEmbedder, build_embedder

    class _Settings:
        embedding_backend = "voyage"
        llm_key_voyage = SecretStr("placeholder")
        voyage_base_url = "https://api.voyageai.com/v1"
        outbound_http_timeout_seconds = 5.0

    embedder, reason = build_embedder(_Settings())
    assert embedder is None
    assert "placeholder" in reason

    class _Configured(_Settings):
        llm_key_voyage = SecretStr("a-real-looking-key")

    configured, reason = build_embedder(_Configured())
    assert isinstance(configured, VoyageEmbedder)
    assert reason == ""
    # D-2 fixes the 1536-d column at Voyage Code 3, and the id is what the row records.
    assert configured.dimensions == 1536
    assert configured.model_id == "voyage-code-3"
    assert configured.table == "embeddings"


def test_the_self_hosted_backend_reports_that_it_has_no_endpoint() -> None:
    """D-48's second table is 1024-d and no setting names a BGE-M3 endpoint yet.

    Saying so is the honest answer; inventing a default URL would turn a missing
    configuration into a connection error on every scan.
    """
    from src.analysis.indexer import build_embedder

    class _Settings:
        embedding_backend = "bge_m3"

    embedder, reason = build_embedder(_Settings())
    assert embedder is None
    assert "1024" in reason
