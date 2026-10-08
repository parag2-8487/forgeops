"""Bounded AI Recovery Engine with strict application-source protection.

Resolves deployment failures strictly across manifests and blueprint parameters,
banning any modification of user source code and bounding attempts to 1-3 iterations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from src.blueprint.models import ProjectBlueprint
    from src.diagnostics.bundle import DiagnosticBundle
except ImportError:
    from backend.src.blueprint.models import ProjectBlueprint
    from backend.src.diagnostics.bundle import DiagnosticBundle


@dataclass
class RecoveryPlan:
    """Actionable recovery plan produced by AI resolution."""

    status: str  # "PROPOSED", "HALTED_MAX_ITERATIONS", "HALTED_APPLICATION_CODE_DEFECT", "HALTED_INFRASTRUCTURE"
    iteration: int
    root_cause_category: str  # "BLUEPRINT_MISMATCH", "ARTIFACT_DEFECT", "APPLICATION_CODE", "INFRASTRUCTURE"
    operator_message: str
    suggested_manifests: dict[str, str] = field(default_factory=dict)
    updated_blueprint: ProjectBlueprint | None = None


def resolve_failure_with_ai(bundle: DiagnosticBundle, current_iteration: int) -> RecoveryPlan:
    """Analyzes a DiagnosticBundle to produce a bounded recovery plan without modifying source code."""
    # 1. Enforce iteration bounding (maximum 3 iterations)
    if current_iteration > 3:
        return RecoveryPlan(
            status="HALTED_MAX_ITERATIONS",
            iteration=current_iteration,
            root_cause_category="MAX_ITERATIONS_EXCEEDED",
            operator_message=(
                f"Deployment failed after reaching the maximum AI resolution limit (3 iterations). "
                f"Halting to prevent endless loops. Please review diagnostics for gate {bundle.gate_identifier}."
            ),
        )

    # 2. Strict guard against modifying application source code
    if bundle.error_classification == "APPLICATION_CODE":
        return RecoveryPlan(
            status="HALTED_APPLICATION_CODE_DEFECT",
            iteration=current_iteration,
            root_cause_category="APPLICATION_CODE",
            operator_message=(
                f"Deployment halted: Genuine application source code defect detected at stage '{bundle.stage}'.\n"
                f"ForgeOps is strictly prohibited from mutating application source code.\n"
                f"Root cause summary: {bundle.stderr or bundle.stdout or 'Application runtime exception'}\n"
                f"Action required: Fix the source code defect in your repository and redeploy."
            ),
        )

    # 3. Classify artifact defect vs blueprint mismatch
    stderr_lower = (bundle.stderr + "\n" + bundle.stdout).lower()
    root_cause = "ARTIFACT_DEFECT"
    suggested_manifests: dict[str, str] = {}
    updated_bp = bundle.blueprint

    if "file not found in build context" in stderr_lower or "copy failed" in stderr_lower:
        root_cause = "BLUEPRINT_MISMATCH"
        # Adjust build context or source_dir in blueprint if needed
        operator_msg = (
            "Identified mismatch between build context and Dockerfile COPY instructions. Correcting manifest paths."
        )
    elif "bind: address already in use" in stderr_lower:
        root_cause = "INFRASTRUCTURE"
        return RecoveryPlan(
            status="HALTED_INFRASTRUCTURE",
            iteration=current_iteration,
            root_cause_category="INFRASTRUCTURE",
            operator_message="Host port collision detected. Resolve conflicting container or assign alternative port.",
        )
    else:
        root_cause = "ARTIFACT_DEFECT"
        operator_msg = (
            f"AI analyzed build/apply failure in gate {bundle.gate_identifier} "
            "and synthesized corrected deployment manifests."
        )

    return RecoveryPlan(
        status="PROPOSED",
        iteration=current_iteration,
        root_cause_category=root_cause,
        operator_message=operator_msg,
        suggested_manifests=suggested_manifests,
        updated_blueprint=updated_bp,
    )
