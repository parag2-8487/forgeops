# SPDX-License-Identifier: FSL-1.1-ALv2
"""Ordered stage-agnostic pipeline runner (Design §11.9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import Finding, PlanDocument, Severity, Stage, StageContext
from .semantic import BlastRadius, SemanticPlanAnalyzer

if TYPE_CHECKING:
    from .approval import ApprovalDecision, ApprovalGate


@dataclass(frozen=True)
class PipelineResult:
    findings: tuple[Finding, ...]
    fatal: bool
    stages_run: tuple[str, ...]
    blast_radius: BlastRadius | None = None
    approval_decision: ApprovalDecision | None = None


class ValidationPipeline:
    """Ordered stages with fatal short-circuit and non-fatal accumulation.

    Phase 0 stages: Syntax -> Schema. The Semantic stage (task 14.2) and
    the Phase 1 DryRun stage slot in without changing this runner.
    """

    def __init__(
        self,
        stages: list[Stage] | None = None,
        *,
        analyzer: SemanticPlanAnalyzer | None = None,
        gate: ApprovalGate | None = None,
    ) -> None:
        if stages is None:
            from .stages import SchemaStage, SyntaxStage

            stages = [SyntaxStage(), SchemaStage()]
        self._stages = stages
        self._analyzer = analyzer
        self._gate = gate

    async def run(self, doc: PlanDocument) -> PipelineResult:
        ctx = StageContext()
        stages_run: list[str] = []

        for stage in self._stages:
            findings = await stage.run(doc, ctx)
            ctx.findings.extend(findings)
            stages_run.append(stage.name)

            # Check for fatal findings — short-circuit
            if any(f.severity == Severity.FATAL for f in findings):
                ctx.fatal = True
                break

        blast_radius = None
        approval_decision = None

        if not ctx.fatal and self._analyzer:
            blast_radius = self._analyzer.analyse(doc)

        if blast_radius and self._gate:
            approval_decision = await self._gate.submit(blast_radius, ctx)

        return PipelineResult(
            findings=tuple(ctx.findings),
            fatal=ctx.fatal,
            stages_run=tuple(stages_run),
            blast_radius=blast_radius,
            approval_decision=approval_decision,
        )
