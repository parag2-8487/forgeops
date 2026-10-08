"""Level 2 Semantic Blueprint Compatibility validation for existing deployment artifacts.

Ensures that existing Dockerfiles and Compose files align with the authoritative
ProjectBlueprint before authorizing their reuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.src.blueprint.models import ProjectBlueprint, WorkloadType
from backend.src.validation.syntax_validator import (
    ValidationResult,
    validate_compose_schema,
    validate_dockerfile_syntax,
)


@dataclass
class CompatibilityResult:
    """Outcome of Level 2 semantic blueprint compatibility check."""
    is_compatible: bool
    artifact_type: str
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def validate_dockerfile_blueprint_compatibility(
    artifact_path: str | Path,
    blueprint: ProjectBlueprint,
) -> CompatibilityResult:
    """Verifies that an existing Dockerfile satisfies the ProjectBlueprint."""
    syntax_res = validate_dockerfile_syntax(artifact_path)
    if not syntax_res.is_valid:
        return CompatibilityResult(
            is_compatible=False,
            artifact_type="dockerfile",
            reasons=[f"Level 1 syntax validation failed: {'; '.join(syntax_res.errors)}"],
            details={"syntax_errors": syntax_res.errors},
        )

    meta = syntax_res.parsed_metadata
    reasons: List[str] = []

    # 1. Base Image Language Compatibility
    base_images = meta.get("base_images", [])
    lang = blueprint.runtime.language.lower()
    compatible_image_found = False

    for img in base_images:
        img_lower = img.lower()
        if lang in {"nodejs", "node", "javascript", "typescript"} and ("node" in img_lower or "bun" in img_lower):
            compatible_image_found = True
        elif lang in {"python", "py"} and "python" in img_lower:
            compatible_image_found = True
        elif lang in {"golang", "go"} and ("golang" in img_lower or "alpine" in img_lower or "scratch" in img_lower):
            compatible_image_found = True
        elif lang in {"rust"} and ("rust" in img_lower or "debian" in img_lower or "alpine" in img_lower):
            compatible_image_found = True
        elif blueprint.workload_type == WorkloadType.STATIC_SPA and ("nginx" in img_lower or "caddy" in img_lower or "node" in img_lower):
            compatible_image_found = True

    if not compatible_image_found and lang not in {"generic"}:
        reasons.append(
            f"Base image(s) {base_images} do not match blueprint runtime language '{lang}'"
        )

    # 2. Port Alignment
    if blueprint.network.listen_port:
        target_port = blueprint.network.listen_port
        exposed_ports = meta.get("exposed_ports", [])
        if exposed_ports and target_port not in exposed_ports:
            # If Dockerfile exposes ports, none of them match the blueprint target port
            reasons.append(
                f"Exposed port(s) {exposed_ports} in Dockerfile do not match blueprint listen_port {target_port}"
            )

    # 3. Working Directory and Context Alignment for Subdirectory Apps
    source_dir = blueprint.build_config.source_dir
    if source_dir not in {".", "./", ""}:
        workdirs = meta.get("workdirs", [])
        copies = meta.get("copy_instructions", [])
        # Dockerfile must reference the subdirectory or its files
        has_subfolder_reference = any(source_dir in w for w in workdirs) or any(source_dir in c for c in copies)
        if not has_subfolder_reference:
            reasons.append(
                f"Application is located in subfolder '{source_dir}', but Dockerfile does not reference this path."
            )

    is_compatible = len(reasons) == 0
    return CompatibilityResult(
        is_compatible=is_compatible,
        artifact_type="dockerfile",
        reasons=reasons,
        details={"metadata": meta, "blueprint_id": blueprint.blueprint_id},
    )


def validate_compose_blueprint_compatibility(
    artifact_path: str | Path,
    blueprint: ProjectBlueprint,
) -> CompatibilityResult:
    """Verifies that an existing Compose file aligns with the ProjectBlueprint."""
    syntax_res = validate_compose_schema(artifact_path)
    if not syntax_res.is_valid:
        return CompatibilityResult(
            is_compatible=False,
            artifact_type="compose",
            reasons=[f"Level 1 syntax validation failed: {'; '.join(syntax_res.errors)}"],
            details={"syntax_errors": syntax_res.errors},
        )

    meta = syntax_res.parsed_metadata
    reasons: List[str] = []
    services = meta.get("services", {})

    # Check port mapping alignment
    if blueprint.network.listen_port:
        target_port = blueprint.network.listen_port
        port_found = False
        for svc_name, svc_info in services.items():
            ports = svc_info.get("ports", [])
            for p in ports:
                p_str = str(p)
                if f":{target_port}" in p_str or p_str == str(target_port):
                    port_found = True
                    break
            if port_found:
                break

        if not port_found and services:
            reasons.append(
                f"Compose services do not map blueprint listen_port {target_port}"
            )

    is_compatible = len(reasons) == 0
    return CompatibilityResult(
        is_compatible=is_compatible,
        artifact_type="compose",
        reasons=reasons,
        details={"metadata": meta, "blueprint_id": blueprint.blueprint_id},
    )
