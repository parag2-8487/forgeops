"""G2 Existing Artifact Gate for ForgeOps.

Coordinates Level 1 and Level 2 validation of pre-existing Docker and Compose artifacts
to decide whether to reuse them safely or proceed to blueprint-grounded synthesis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from src.blueprint.models import ProjectBlueprint
    from src.validation.blueprint_compatibility import (
        validate_compose_blueprint_compatibility,
        validate_dockerfile_blueprint_compatibility,
    )
except ImportError:
    from backend.src.blueprint.models import ProjectBlueprint
    from backend.src.validation.blueprint_compatibility import (
        validate_compose_blueprint_compatibility,
        validate_dockerfile_blueprint_compatibility,
    )


@dataclass
class ArtifactGateDecision:
    """Decision output of the G2 Existing Artifact Gate."""

    gate_id: str = "G2"
    action: str = "REGENERATE"  # "REUSE" or "REGENERATE"
    reusable_dockerfile: str | None = None
    reusable_compose: str | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def evaluate_existing_artifact_gate(
    repo_path: str | Path,
    blueprint: ProjectBlueprint,
) -> ArtifactGateDecision:
    """Evaluates whether pre-existing deployment artifacts satisfy Level 1 & Level 2 gates."""
    root = Path(repo_path).resolve()
    decision = ArtifactGateDecision()

    # Potential Dockerfile locations: root or blueprint.source_dir
    possible_dockerfiles = [
        root / "Dockerfile",
        (root / blueprint.build_config.source_dir / "Dockerfile").resolve(),
    ]

    valid_dockerfile: Path | None = None
    for df in possible_dockerfiles:
        if df.exists() and df.is_file():
            comp_res = validate_dockerfile_blueprint_compatibility(df, blueprint)
            if comp_res.is_compatible:
                valid_dockerfile = df
                decision.details["dockerfile_validation"] = "PASSED"
                break
            else:
                decision.rejection_reasons.extend(
                    [f"Existing Dockerfile at {df.name} rejected: {r}" for r in comp_res.reasons]
                )

    # Potential Compose locations
    possible_compose = [
        root / "docker-compose.yml",
        root / "docker-compose.yaml",
        root / "compose.yml",
        root / "compose.yaml",
    ]

    valid_compose: Path | None = None
    for cf in possible_compose:
        if cf.exists() and cf.is_file():
            comp_res = validate_compose_blueprint_compatibility(cf, blueprint)
            if comp_res.is_compatible:
                valid_compose = cf
                decision.details["compose_validation"] = "PASSED"
                break
            else:
                decision.rejection_reasons.extend(
                    [f"Existing Compose file at {cf.name} rejected: {r}" for r in comp_res.reasons]
                )

    # Determine overall action
    if valid_dockerfile and valid_compose:
        decision.action = "REUSE"
        decision.reusable_dockerfile = str(valid_dockerfile)
        decision.reusable_compose = str(valid_compose)
    elif valid_dockerfile:
        decision.action = "REUSE_DOCKERFILE_ONLY"
        decision.reusable_dockerfile = str(valid_dockerfile)
    else:
        decision.action = "REGENERATE"

    return decision
