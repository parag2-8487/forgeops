# SPDX-License-Identifier: FSL-1.1-ALv2
"""Blocking gate vs Advisory rubric isolation (Leaf 13.5)."""

from __future__ import annotations

from pydantic import BaseModel


class GateResult(BaseModel):
    passed: bool
    violations: list[str]


class RubricResult(BaseModel):
    advisory_score: float
    suggestions: list[str]


class BlockingGate:
    """Strict binary gate for security, syntax, and schema errors.

    Failure MUST block artifact admission.
    """

    def evaluate(self, content: str) -> GateResult:
        violations: list[str] = []
        if "eval(" in content or "exec(" in content:
            violations.append("Forbidden unsafe call detected")
        if "rm -rf /" in content:
            violations.append("Forbidden destructive shell command")

        return GateResult(passed=len(violations) == 0, violations=violations)


class AdvisoryRubric:
    """Style and quality advisory evaluator.

    Outputs advice only; NEVER mutates or overrides BlockingGate decisions.
    """

    def evaluate(self, content: str) -> RubricResult:
        suggestions: list[str] = []
        score = 100.0

        if "#" not in content and "//" not in content:
            score -= 10.0
            suggestions.append("Consider adding comments to explain complex logic")

        return RubricResult(advisory_score=score, suggestions=suggestions)
