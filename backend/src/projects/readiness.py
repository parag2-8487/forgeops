# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deterministic deployment-readiness scoring engine (Leaf 12.3)."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel


class ReadinessBreakdown(BaseModel):
    documentation_score: int
    test_coverage_score: int
    ci_config_score: int
    security_policy_score: int
    containerization_score: int


class ReadinessResult(BaseModel):
    overall_score: int
    level: str  # "production_ready", "needs_improvement", "blocked"
    breakdown: ReadinessBreakdown
    recommendations: list[str]


class ReadinessEngine:
    """Deterministic readiness scoring engine."""

    def evaluate_project(self, project_data: dict[str, Any]) -> ReadinessResult:
        manifests = project_data.get("manifests", [])
        config_files = project_data.get("config_files", [])
        has_tests = project_data.get("has_tests", True)
        has_docker = "Dockerfile" in manifests or any("Dockerfile" in c for c in config_files)
        has_ci = any(".github" in c or "ci" in c for c in config_files)

        doc_score = 20 if any("README" in c or "docs" in c for c in config_files) else 10
        test_score = 25 if has_tests else 0
        ci_score = 20 if has_ci else 5
        sec_score = 15 if any("policy" in c or "SECURITY" in c for c in config_files) else 5
        container_score = 20 if has_docker else 5

        total = doc_score + test_score + ci_score + sec_score + container_score
        total = min(max(total, 0), 100)

        if total >= 80:
            level = "production_ready"
        elif total >= 50:
            level = "needs_improvement"
        else:
            level = "blocked"

        recommendations = []
        if not has_docker:
            recommendations.append("Add a Dockerfile for reproducible container builds.")
        if not has_ci:
            recommendations.append("Configure continuous integration workflows.")

        return ReadinessResult(
            overall_score=total,
            level=level,
            breakdown=ReadinessBreakdown(
                documentation_score=doc_score,
                test_coverage_score=test_score,
                ci_config_score=ci_score,
                security_policy_score=sec_score,
                containerization_score=container_score,
            ),
            recommendations=recommendations,
        )
