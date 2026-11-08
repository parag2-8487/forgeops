"""G1 Blueprint Gate implementation for ForgeOps.

Asserts manifest consistency, valid runtime, real entrypoints, valid project roots,
and complete build/start contracts without hardcoded path assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from src.blueprint.models import ProjectBlueprint, WorkloadType
except ImportError:
    from backend.src.blueprint.models import ProjectBlueprint, WorkloadType


@dataclass
class GateResult:
    """Standardized result of a pipeline verification gate."""

    gate_id: str
    passed: bool
    status: str  # "PASSED", "FAILED", "AWAITING_OPERATOR_INPUT"
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)


def verify_blueprint_gate(blueprint: ProjectBlueprint) -> GateResult:
    """Verifies that the synthesized ProjectBlueprint satisfies all G1 criteria."""
    # 1. Check ambiguity
    if blueprint.ambiguity.is_ambiguous:
        return GateResult(
            gate_id="G1",
            passed=False,
            status="AWAITING_OPERATOR_INPUT",
            message=blueprint.ambiguity.unresolved_reason or "Ambiguous deployment targets detected in repository.",
            details={
                "strategy": blueprint.ambiguity.resolution_strategy,
                "confidence_score": blueprint.ambiguity.confidence_score,
            },
            candidates=list(blueprint.ambiguity.detected_candidates),
        )

    repo_root = Path(blueprint.repository_root)

    # 2. Check repository root exists
    if not repo_root.exists() or not repo_root.is_dir():
        return GateResult(
            gate_id="G1",
            passed=False,
            status="FAILED",
            message=f"Repository root does not exist on disk: {blueprint.repository_root}",
            details={"repository_root": blueprint.repository_root},
        )

    # 3. Check source_dir exists
    source_path = (repo_root / blueprint.build_config.source_dir).resolve()
    if not source_path.exists() or not source_path.is_dir():
        return GateResult(
            gate_id="G1",
            passed=False,
            status="FAILED",
            message=f"Blueprint source_dir does not exist on disk: {blueprint.build_config.source_dir}",
            details={"source_path": str(source_path)},
        )

    # 4. Check runtime language and start command
    if not blueprint.runtime.language or blueprint.runtime.language == "generic":
        return GateResult(
            gate_id="G1",
            passed=False,
            status="FAILED",
            message="No valid programming runtime or language identified in blueprint.",
            details={"runtime": blueprint.runtime.to_dict()},
        )

    if not blueprint.runtime.start_command and blueprint.workload_type != WorkloadType.BATCH_JOB:
        return GateResult(
            gate_id="G1",
            passed=False,
            status="FAILED",
            message="Blueprint lacks a runnable start_command or entrypoint.",
            details={"runtime": blueprint.runtime.to_dict()},
        )

    # 5. Check network contract for web services
    if blueprint.workload_type in {WorkloadType.WEB_SERVICE, WorkloadType.STATIC_SPA}:
        if not blueprint.network.listen_port:
            return GateResult(
                gate_id="G1",
                passed=False,
                status="FAILED",
                message=(
                    f"Workload {blueprint.workload_type.value} requires an exposed listen_port in network contract."
                ),
                details={"network": blueprint.network.to_dict()},
            )

    fw = blueprint.runtime.framework or "standard"
    return GateResult(
        gate_id="G1",
        passed=True,
        status="PASSED",
        message=(
            f"Blueprint verified successfully for {blueprint.runtime.language} ({fw}) "
            f"at source_dir '{blueprint.build_config.source_dir}'."
        ),
        details={
            "source_dir": blueprint.build_config.source_dir,
            "workload_type": blueprint.workload_type.value,
            "framework": blueprint.runtime.framework,
            "listen_port": blueprint.network.listen_port,
        },
    )
