# SPDX-License-Identifier: FSL-1.1-ALv2
"""Approval gate seam connecting the validation pipeline to the approval workflow.

Phase 0 implementation: a pure verdict -> decision mapping.
Phase 1 adds the Change Approval Center with persisted change-sets.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from .models import StageContext
from .semantic import BlastRadius


class ApprovalDecision(StrEnum):
    AUTO_OK = "auto_ok"
    REQUIRES_APPROVAL = "requires_approval"
    BLOCKED = "blocked"


class ApprovalGate(Protocol):
    """The approval gate interface. Phase 1 replaces ThresholdApprovalGate."""

    async def submit(self, verdict: BlastRadius, ctx: StageContext) -> ApprovalDecision: ...


class ThresholdApprovalGate:
    """Phase 0 implementation: pure verdict -> decision mapping.

    allow -> AUTO_OK
    warn -> REQUIRES_APPROVAL
    block -> BLOCKED
    """

    async def submit(self, verdict: BlastRadius, ctx: StageContext) -> ApprovalDecision:
        match verdict.verdict:
            case "allow":
                return ApprovalDecision.AUTO_OK
            case "warn":
                return ApprovalDecision.REQUIRES_APPROVAL
            case "block":
                return ApprovalDecision.BLOCKED
            case _:
                return ApprovalDecision.BLOCKED  # fail-closed
