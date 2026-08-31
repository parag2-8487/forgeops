# SPDX-License-Identifier: FSL-1.1-ALv2
"""Plan analysis API endpoint (Design §14.4).

POST /api/v1/analysis/plan — accepts plan JSON, runs the validation pipeline
with SemanticPlanAnalyzer + ThresholdApprovalGate, and returns findings,
blast_radius, verdict, and approval_decision.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.device_dependencies import require_device
from ..auth.principal import Principal
from ..core.db import get_session
from ..core.errors import forbidden_problem
from ..core.tenancy import load_visible_project
from .indexer import IndexResult, ScanReportIn, build_embedder, persist_scan_report
from .plan_analyzer import (
    PlanDocument,
    SemanticPlanAnalyzer,
    ThresholdApprovalGate,
    ValidationPipeline,
)
from .plan_analyzer.semantic import SemanticStage
from .plan_analyzer.stages import SchemaStage, SyntaxStage

router = APIRouter(
    prefix="/api/v1/analysis",
    tags=["analysis"],
    # Deny by default (§4.4). Attached at the ROUTER, which FastAPI applies to every
    # route it carries, so a new endpoint added here is protected the moment it is
    # declared rather than when someone remembers. Still per-route in effect — not a
    # global dependency or a middleware, either of which would have to carve out the
    # public set by path matching, and a path matcher is where an unauthenticated route
    # hides. `scripts/check-route-auth.py` asserts the result over the real router.
    dependencies=[Depends(require_principal)],
)

# A SECOND ROUTER, for the one route an AGENT calls rather than a user.
#
# Same prefix and tag, so the API surface is unchanged from a caller's point of view. Separate
# because the dependency above is attached at the ROUTER and FastAPI applies it to every route the
# router carries — which is exactly the property that makes deny-by-default work, and exactly why
# a route with a different authentication mechanism cannot live on it. Overriding per route would
# mean the router-level dependency still ran first and still refused the agent.
#
# The alternative considered was one router with a dependency that accepted either credential. It
# was rejected: a route's authentication would then be decided by which of two branches happened to
# match, and a bug in that branch would silently widen every route on the router at once. Two
# routers make each route's mechanism a structural fact.
agent_router = APIRouter(
    prefix="/api/v1/analysis",
    tags=["analysis"],
    dependencies=[Depends(require_device)],
)


class PlanAnalysisRequest(BaseModel):
    """Request body for plan analysis."""

    plan: dict[str, Any] = Field(..., description="OpenTofu/Terraform plan JSON object")


class FindingResponse(BaseModel):
    stage: str
    severity: str
    code: str
    message: str
    resource: str | None = None


class BlastRadiusResponse(BaseModel):
    score: int
    destructive_count: int
    affected_resources: int
    stateful_deletions: list[str]
    verdict: str


class PlanAnalysisResponse(BaseModel):
    findings: list[FindingResponse]
    blast_radius: BlastRadiusResponse | None = None
    verdict: str
    approval_decision: str | None = None


@router.post("/plan", response_model=PlanAnalysisResponse)
async def analyse_plan(body: PlanAnalysisRequest) -> PlanAnalysisResponse:
    """Analyse a Terraform/OpenTofu plan and return findings + blast radius."""
    # Parse the plan document
    try:
        doc = PlanDocument.from_json(
            # PlanDocument.from_json expects str/bytes, so serialize back
            __import__("json").dumps(body.plan)
        )
    except ValueError as exc:
        from core.errors import ProblemException

        raise ProblemException(
            status=422,
            type_suffix="invalid-plan-document",
            title="Invalid plan document",
            detail=str(exc),
        ) from exc

    # Build pipeline with semantic analyzer and approval gate
    analyzer = SemanticPlanAnalyzer()
    gate = ThresholdApprovalGate()
    pipeline = ValidationPipeline(
        stages=[SyntaxStage(), SchemaStage(), SemanticStage(analyzer)],
        analyzer=analyzer,
        gate=gate,
    )

    result = await pipeline.run(doc)

    # Build response
    findings = [
        FindingResponse(
            stage=f.stage,
            severity=f.severity.value,
            code=f.code,
            message=f.message,
            resource=f.resource,
        )
        for f in result.findings
    ]

    blast_radius = None
    if result.blast_radius:
        blast_radius = BlastRadiusResponse(
            score=result.blast_radius.score,
            destructive_count=result.blast_radius.destructive_count,
            affected_resources=result.blast_radius.affected_resources,
            stateful_deletions=list(result.blast_radius.stateful_deletions),
            verdict=result.blast_radius.verdict,
        )

    # Determine overall verdict
    if result.fatal:
        verdict = "fatal"
    elif result.blast_radius:
        verdict = result.blast_radius.verdict
    else:
        verdict = "allow"

    approval_decision = result.approval_decision.value if result.approval_decision else None

    return PlanAnalysisResponse(
        findings=findings,
        blast_radius=blast_radius,
        verdict=verdict,
        approval_decision=approval_decision,
    )


# ─── Codebase Index API (Leaf 11.8, phases.md §1.3) ──────────────────────────
#
# All three read endpoints previously returned LITERALS on a live route:
# `indexed_files=42, total_chunks=128, status="ready"`, a `NewParser` symbol at a fixed
# line, and a chunk body of `"func NewParser() ..."` for any chunk id at all. That is the
# worst shape a defect can take here, because the answer asserted that an index existed
# while `file_tree`, `file_contents` and `embeddings` were empty — a caller could not tell
# a real index from none, and the readiness screen and retrieval both read through this
# surface.
#
# They are now queries. Every one is scoped by project id and by the caller's tenant, and
# an unindexed project answers with zeros, an empty list, or the non-disclosing 403 —
# never with a number nobody counted.


class CodebaseStatusResponse(BaseModel):
    indexed_files: int
    total_chunks: int
    languages: list[str]
    #: `empty` — nothing indexed. `indexed_without_vectors` — tree and contents stored but
    #: no embeddings, which is what an unavailable embedding provider honestly looks like
    #: and which means retrieval is sparse-only. `indexed` — both.
    status: str
    total_bytes: int = 0
    resolved_dependencies: int = 0
    unresolved_dependencies: int = 0
    last_indexed_at: datetime | None = None


class SymbolQueryResponse(BaseModel):
    name: str
    kind: str
    file_path: str
    line_number: int
    parent_symbol: str | None = None
    signature: str | None = None
    chunk_id: uuid.UUID


class ChunkDetailResponse(BaseModel):
    chunk_id: str
    file_path: str
    content: str
    start_line: int
    end_line: int
    language: str
    symbol: str | None = None
    parent_symbol: str | None = None
    kind: str | None = None
    token_count: int | None = None
    model_id: str


async def _require_visible_project(
    session: AsyncSession, *, project_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> None:
    """Refuse the request unless the caller's tenant may see this project.

    Delegates to `projects.load_visible_project` rather than re-deriving the rule, so the
    index surface and the project surface cannot disagree about who may read a project.
    A 403 whose body is identical for "no such project" and "another tenant's project" is
    required by §4.2 and Q-20: any difference is an enumeration oracle for project ids.
    """
    await load_visible_project(session, project_id=project_id, tenant_id=tenant_id)


@agent_router.post(
    "/codebase/{project_id}/index",
    response_model=IndexResult,
    summary="Persist an agent scan report into the codebase index",
)
async def index_scan_report(
    project_id: uuid.UUID,
    report: ScanReportIn,
    request: Request,
    device: Annotated[Any, Depends(require_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IndexResult:
    """Write a scan report into `file_tree`, `file_contents`, `file_dependencies` and the
    vector tables.

    This endpoint is what makes the index non-empty; nothing else writes these tables. The
    content it stores is already redacted — the agent redacts before serialising, because
    `file_contents` is a redacted-only store (design §6.3, §7.11) and redaction after
    transmission would mean the unredacted text had already left the machine.

    Vectors are written only when an embedding provider is configured. When there is none,
    the tree, the contents and the dependency graph are still persisted and
    `vectors_absent_reason` says why retrieval will be sparse-only — a zero or random
    vector would be indistinguishable from a real one at query time.

    AUTHENTICATED AS A DEVICE, NOT AS A USER, and this is the only route in the module that is.
    An agent cannot satisfy `require_principal` — that verifies a user OIDC token through JWKS —
    so while this route sat behind it the agent's scan submit was refused with
    `Unauthenticated` after doing all of the work. `require_device` requires the client
    certificate AND the token, the same two factors the WebSocket handshake requires, because a
    token-only door here would be the softer one for the same credential.

    SCOPED TO THE DEVICE'S OWN PROJECT. The path carries a project id, and the authenticated
    device is paired to exactly one; a device that could index any id could overwrite another
    tenant's index with its own workspace. The mismatch answers with the same non-disclosing 403
    the read routes use, so it cannot be used to discover which project ids exist.
    """
    if device.project_id != project_id:
        raise forbidden_problem()
    embedder, reason = build_embedder(request.app.state.settings)
    result = await persist_scan_report(
        session,
        project_id=project_id,
        tenant_id=device.tenant_id,
        report=report,
        embedder=embedder,
        embedder_absent_reason=reason,
    )
    # COMMITTED HERE, not left to the dependency's teardown, because success has to mean DURABLE.
    #
    # `get_session` commits after `yield session` returns, and FastAPI runs a `yield` dependency's
    # exit code AFTER the response has been sent. So the client received `200 {"files_indexed": 3}`
    # while the rows were still uncommitted, and a caller that immediately read the index saw
    # nothing. That is exactly what happened: the journey scanned, got a success naming three files,
    # then asked for the readiness score and was told the project had zero indexed paths — and the
    # rows appeared moments later, which made it look like the scorer was broken rather than the
    # ordering.
    #
    # This endpoint is the one where the race matters, because the only reason to call it is so that
    # something else can read what it wrote. The double commit is free: the dependency's own commit
    # finds a clean session and does nothing.
    await session.commit()
    return result


@router.get("/codebase/{project_id}/status", response_model=CodebaseStatusResponse)
async def get_codebase_status(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CodebaseStatusResponse:
    """Report what is actually in the index for one project."""
    await _require_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)

    files = await session.execute(
        text(
            "SELECT count(*) AS files, coalesce(sum(size_bytes), 0) AS bytes, max(last_modified) AS newest "
            "FROM file_tree WHERE project_id = :project_id"
        ),
        {"project_id": project_id},
    )
    file_row = files.mappings().one()

    chunks = await session.execute(
        text(
            "SELECT (SELECT count(*) FROM embeddings e JOIN file_tree f ON f.id = e.file_id "
            "WHERE f.project_id = :project_id) "
            "+ (SELECT count(*) FROM embeddings_local l JOIN file_tree f ON f.id = l.file_id "
            "WHERE f.project_id = :project_id) AS total"
        ),
        {"project_id": project_id},
    )
    total_chunks = int(chunks.scalar() or 0)

    # Languages come from `file_contents`, which is where the agent's tiered detection
    # result was stored. NULL is excluded rather than reported as a language called
    # "unknown", which would be a value the detector never produced.
    languages = await session.execute(
        text(
            "SELECT DISTINCT c.language FROM file_contents c JOIN file_tree f ON f.id = c.file_id "
            "WHERE f.project_id = :project_id AND c.language IS NOT NULL ORDER BY c.language"
        ),
        {"project_id": project_id},
    )

    dependencies = await session.execute(
        text(
            "SELECT coalesce(sum(CASE WHEN resolved THEN 1 ELSE 0 END), 0) AS resolved, "
            "coalesce(sum(CASE WHEN resolved THEN 0 ELSE 1 END), 0) AS unresolved "
            "FROM file_dependencies WHERE project_id = :project_id"
        ),
        {"project_id": project_id},
    )
    dependency_row = dependencies.mappings().one()

    indexed_files = int(file_row["files"] or 0)
    if indexed_files == 0:
        status = "empty"
    elif total_chunks == 0:
        status = "indexed_without_vectors"
    else:
        status = "indexed"

    return CodebaseStatusResponse(
        indexed_files=indexed_files,
        total_chunks=total_chunks,
        languages=[str(row[0]) for row in languages],
        status=status,
        total_bytes=int(file_row["bytes"] or 0),
        resolved_dependencies=int(dependency_row["resolved"] or 0),
        unresolved_dependencies=int(dependency_row["unresolved"] or 0),
        last_indexed_at=file_row["newest"],
    )


@router.get("/codebase/{project_id}/symbols", response_model=list[SymbolQueryResponse])
async def query_symbols(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    query: str = "",
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SymbolQueryResponse]:
    """Substring search over indexed symbols, scoped to one project.

    Reads the cAST metadata revision `0003` added to `embeddings`, so a project whose
    chunks were stored without vectors — the honest outcome when no embedding provider is
    configured — has no symbols to return and gets an empty list. That is the correct
    answer, and it is the one the previous implementation replaced with a fixed
    `NewParser`.
    """
    await _require_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)

    rows = await session.execute(
        text(
            "SELECT e.id, e.symbol, e.parent_symbol, e.signature, e.kind, e.start_line, f.path "
            "FROM embeddings e JOIN file_tree f ON f.id = e.file_id "
            "WHERE f.project_id = :project_id AND e.symbol IS NOT NULL "
            # `position(... in lower(...)) > 0` rather than `LIKE '%' || :q || '%'`, so a
            # `%` or `_` typed into the search box is a literal character rather than a
            # wildcard the caller did not ask for.
            "AND (:query = '' OR position(lower(:query) in lower(e.symbol)) > 0) "
            "ORDER BY length(e.symbol), e.symbol, f.path, e.start_line LIMIT :limit"
        ),
        {"project_id": project_id, "query": query.strip(), "limit": limit},
    )
    return [
        SymbolQueryResponse(
            name=str(row["symbol"]),
            kind=str(row["kind"] or "unknown"),
            file_path=str(row["path"]),
            line_number=int(row["start_line"] or 0),
            parent_symbol=row["parent_symbol"],
            signature=row["signature"],
            chunk_id=row["id"],
        )
        for row in rows.mappings()
    ]


@router.get("/codebase/{project_id}/chunks/{chunk_id}", response_model=ChunkDetailResponse)
async def get_chunk_details(
    project_id: uuid.UUID,
    chunk_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChunkDetailResponse:
    """Return one stored chunk, or the non-disclosing 403.

    A chunk that does not exist and a chunk belonging to another project answer
    identically, for the same reason §4.2 gives for projects: a distinguishable answer is
    an oracle for ids. The content returned is the redacted text that was stored — there
    is no unredacted copy to return.
    """
    await _require_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)

    rows = await session.execute(
        text(
            "SELECT e.id, e.chunk_text, e.start_line, e.end_line, e.symbol, e.parent_symbol, "
            "e.kind, e.token_count, e.model_id, f.path, c.language "
            "FROM embeddings e JOIN file_tree f ON f.id = e.file_id "
            "LEFT JOIN file_contents c ON c.file_id = f.id "
            "WHERE e.id = :chunk_id AND f.project_id = :project_id"
        ),
        {"chunk_id": chunk_id, "project_id": project_id},
    )
    row = rows.mappings().first()
    if row is None:
        raise forbidden_problem()

    return ChunkDetailResponse(
        chunk_id=str(row["id"]),
        file_path=str(row["path"]),
        content=str(row["chunk_text"]),
        start_line=int(row["start_line"] or 0),
        end_line=int(row["end_line"] or 0),
        # The detected language of the file, or the empty string when the file has no
        # `file_contents` row. Not a guess from the extension: the detector's answer is
        # stored, and re-deriving it here could disagree with what the index holds.
        language=str(row["language"] or ""),
        symbol=row["symbol"],
        parent_symbol=row["parent_symbol"],
        kind=row["kind"],
        token_count=row["token_count"],
        model_id=str(row["model_id"]),
    )


class SecretFindingResponse(BaseModel):
    """One file the scanner found secret material in.

    NO VALUE FIELD, and that absence is the requirement rather than a precaution. §7.11 places
    `file_contents` in the "redacted text only" class, and the value did not survive the redaction that
    produced this count — there is nothing to return even if returning it were acceptable. A path and a
    count send an operator to the right file, which is what they need.
    """

    file_path: str
    #: How many distinct secrets were redacted in this file. A count rather than a boolean, because "one
    #: hardcoded token" and "forty" call for different responses.
    redaction_count: int


class SecretScanSummaryResponse(BaseModel):
    """FR-42's answer for one project."""

    project_id: uuid.UUID
    #: True when the project has been indexed at all. An unindexed project has no findings AND no
    #: assurance, and reporting `clean: true` for it would be the fail-open reading.
    indexed: bool
    #: True only when the project is indexed and no file carries a redaction.
    clean: bool
    files_with_findings: int
    total_findings: int
    findings: list[SecretFindingResponse]


@router.get("/codebase/{project_id}/secrets", response_model=SecretScanSummaryResponse)
async def secret_scan_summary(
    project_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(default=100, ge=1, le=500),
) -> SecretScanSummaryResponse:
    """What the secret scan found, per file (FR-42).

    **This endpoint is new, and its absence was the finding.** The agent's scanner runs on every file of
    every index — that is what produces the redacted bodies `file_contents` stores — and
    `file_contents.redaction_count` has recorded the per-file result since revision `0003`. Nothing read
    it. So "secret scanning of the codebase for hardcoded secrets" happened on every scan and left no
    trace an operator could reach, which is a scan whose result is a private fact.

    A GET rather than an operation to dispatch: the scan has already run, and asking the agent to run it
    again would make reading a result a mutation of the workspace. The agent's `secretscan.run` operation
    remains, for the case where an operator wants a fresh scan without a full re-index.
    """
    await _require_visible_project(session, project_id=project_id, tenant_id=principal.tenant_id)

    # Two aggregates and a page, in one statement each. The totals must cover every file rather than the
    # page, because "12 findings" and "the first 100 files had 12" are different claims.
    totals = await session.execute(
        text(
            "SELECT count(*) AS files, coalesce(sum(c.redaction_count), 0) AS total "
            "FROM file_contents c JOIN file_tree f ON f.id = c.file_id "
            "WHERE f.project_id = :project_id AND c.redaction_count > 0"
        ),
        {"project_id": project_id},
    )
    totals_row = totals.mappings().one()

    indexed_row = await session.execute(
        text("SELECT count(*) AS n FROM file_tree WHERE project_id = :project_id"),
        {"project_id": project_id},
    )
    indexed = int(indexed_row.mappings().one()["n"]) > 0

    rows = await session.execute(
        text(
            "SELECT f.path, c.redaction_count FROM file_contents c JOIN file_tree f ON f.id = c.file_id "
            "WHERE f.project_id = :project_id AND c.redaction_count > 0 "
            # Worst first: an operator triaging this wants the file with forty findings, not the
            # alphabetically first one with one.
            "ORDER BY c.redaction_count DESC, f.path LIMIT :limit"
        ),
        {"project_id": project_id, "limit": limit},
    )
    findings = [
        SecretFindingResponse(file_path=str(row["path"]), redaction_count=int(row["redaction_count"]))
        for row in rows.mappings()
    ]

    files_with_findings = int(totals_row["files"] or 0)
    return SecretScanSummaryResponse(
        project_id=project_id,
        indexed=indexed,
        # `clean` requires BOTH indexed and no findings. An unindexed project has no findings and no
        # assurance either, and reporting it clean would be the fail-open reading of an absent scan.
        clean=indexed and files_with_findings == 0,
        files_with_findings=files_with_findings,
        total_findings=int(totals_row["total"] or 0),
        findings=findings,
    )
