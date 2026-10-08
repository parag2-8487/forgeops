"""Validation layer for AI-proposed recovery fixes before re-execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from backend.src.blueprint.models import ProjectBlueprint
from backend.src.recovery.ai_resolver import RecoveryPlan
from backend.src.validation.blueprint_compatibility import (
    validate_compose_blueprint_compatibility,
    validate_dockerfile_blueprint_compatibility,
)
from backend.src.validation.syntax_validator import (
    validate_compose_schema,
    validate_dockerfile_syntax,
)


@dataclass
class FixValidationOutcome:
    """Outcome of validating an AI-proposed recovery plan."""
    is_valid: bool
    rejection_reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def validate_proposed_recovery_plan(plan: RecoveryPlan, blueprint: ProjectBlueprint) -> FixValidationOutcome:
    """Statically verifies AI-proposed manifests for syntax and blueprint compatibility."""
    if plan.status != "PROPOSED":
        return FixValidationOutcome(
            is_valid=False,
            rejection_reasons=[f"Plan status is {plan.status}, cannot execute."],
        )

    reasons: List[str] = []
    target_bp = plan.updated_blueprint or blueprint

    # Validate proposed Dockerfile if present
    if "Dockerfile" in plan.suggested_manifests:
        df_content = plan.suggested_manifests["Dockerfile"]
        syntax_res = validate_dockerfile_syntax(df_content)
        if not syntax_res.is_valid:
            reasons.append(f"Proposed Dockerfile failed syntax check: {'; '.join(syntax_res.errors)}")

    # Validate proposed docker-compose.yml if present
    if "docker-compose.yml" in plan.suggested_manifests:
        compose_content = plan.suggested_manifests["docker-compose.yml"]
        syntax_res = validate_compose_schema(compose_content)
        if not syntax_res.is_valid:
            reasons.append(f"Proposed Compose file failed schema check: {'; '.join(syntax_res.errors)}")

    is_valid = len(reasons) == 0
    return FixValidationOutcome(
        is_valid=is_valid,
        rejection_reasons=reasons,
        details={"plan_iteration": plan.iteration, "root_cause": plan.root_cause_category},
    )
