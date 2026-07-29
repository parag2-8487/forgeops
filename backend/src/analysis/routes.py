# SPDX-License-Identifier: FSL-1.1-ALv2
"""Plan analysis API endpoint (Design §14.4).

POST /api/v1/analysis/plan — accepts plan JSON, runs the validation pipeline
with SemanticPlanAnalyzer + ThresholdApprovalGate, and returns findings,
blast_radius, verdict, and approval_decision.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .plan_analyzer import (
    PlanDocument,
    SemanticPlanAnalyzer,
    ThresholdApprovalGate,
    ValidationPipeline,
)
from .plan_analyzer.semantic import SemanticStage
from .plan_analyzer.stages import SchemaStage, SyntaxStage

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis"])


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
