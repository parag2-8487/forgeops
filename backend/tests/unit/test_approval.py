# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for the approval gate seam (task 14.3)."""

import json

import pytest
from src.analysis.plan_analyzer import (
    PlanDocument,
    SchemaStage,
    SyntaxStage,
    ValidationPipeline,
)
from src.analysis.plan_analyzer.approval import (
    ApprovalDecision,
    ThresholdApprovalGate,
)
from src.analysis.plan_analyzer.models import StageContext
from src.analysis.plan_analyzer.semantic import (
    BlastRadius,
    SemanticPlanAnalyzer,
)


class TestThresholdApprovalGate:
    @pytest.mark.asyncio
    async def test_allow_maps_to_auto_ok(self):
        gate = ThresholdApprovalGate()
        verdict = BlastRadius(
            score=5, destructive_count=0, affected_resources=1, stateful_deletions=(), verdict="allow"
        )
        decision = await gate.submit(verdict, StageContext())
        assert decision == ApprovalDecision.AUTO_OK

    @pytest.mark.asyncio
    async def test_warn_maps_to_requires_approval(self):
        gate = ThresholdApprovalGate()
        verdict = BlastRadius(
            score=15, destructive_count=1, affected_resources=2, stateful_deletions=(), verdict="warn"
        )
        decision = await gate.submit(verdict, StageContext())
        assert decision == ApprovalDecision.REQUIRES_APPROVAL

    @pytest.mark.asyncio
    async def test_block_maps_to_blocked(self):
        gate = ThresholdApprovalGate()
        verdict = BlastRadius(
            score=30, destructive_count=2, affected_resources=3, stateful_deletions=("aws_db.prod",), verdict="block"
        )
        decision = await gate.submit(verdict, StageContext())
        assert decision == ApprovalDecision.BLOCKED

    @pytest.mark.asyncio
    async def test_unknown_verdict_fails_closed(self):
        gate = ThresholdApprovalGate()
        # Construct with an unexpected verdict string
        verdict = BlastRadius(
            score=0, destructive_count=0, affected_resources=0, stateful_deletions=(), verdict="unknown"
        )
        decision = await gate.submit(verdict, StageContext())
        assert decision == ApprovalDecision.BLOCKED


class TestPipelineWithApproval:
    @pytest.mark.asyncio
    async def test_analyzer_verdict_reaches_gate(self):
        pipeline = ValidationPipeline(
            [SyntaxStage(), SchemaStage()],
            analyzer=SemanticPlanAnalyzer(),
            gate=ThresholdApprovalGate(),
        )
        doc = PlanDocument.from_json(
            json.dumps(
                {
                    "format_version": "1.2",
                    "terraform_version": "1.12.5",
                    "resource_changes": [
                        {
                            "address": "null_resource.test",
                            "type": "null_resource",
                            "change": {"actions": ["create"]},
                        }
                    ],
                }
            )
        )
        result = await pipeline.run(doc)
        assert result.blast_radius is not None
        assert result.approval_decision == ApprovalDecision.AUTO_OK

    @pytest.mark.asyncio
    async def test_fatal_skips_analyzer_and_gate(self):
        pipeline = ValidationPipeline(
            [SyntaxStage(), SchemaStage()],
            analyzer=SemanticPlanAnalyzer(),
            gate=ThresholdApprovalGate(),
        )
        doc = PlanDocument(raw={}, format_version="", terraform_version="")
        result = await pipeline.run(doc)
        assert result.fatal
        assert result.blast_radius is None
        assert result.approval_decision is None

    @pytest.mark.asyncio
    async def test_no_analyzer_no_gate(self):
        pipeline = ValidationPipeline([SyntaxStage(), SchemaStage()])
        doc = PlanDocument.from_json(
            json.dumps(
                {
                    "format_version": "1.2",
                    "terraform_version": "1.12.5",
                    "resource_changes": [],
                }
            )
        )
        result = await pipeline.run(doc)
        assert result.blast_radius is None
        assert result.approval_decision is None
