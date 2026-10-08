"""Level 1 Syntax and Schema validation for existing deployment artifacts.

Parses Dockerfiles, Compose files, and Kubernetes manifests for structural correctness
before evaluating semantic blueprint compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ValidationResult:
    """Outcome of a syntax or schema validation check."""

    is_valid: bool
    artifact_type: str  # "dockerfile", "compose", "kubernetes"
    message: str
    errors: list[str] = field(default_factory=list)
    parsed_metadata: dict[str, Any] = field(default_factory=dict)


DOCKERFILE_INSTRUCTIONS = {
    "FROM",
    "RUN",
    "CMD",
    "LABEL",
    "EXPOSE",
    "ENV",
    "ADD",
    "COPY",
    "ENTRYPOINT",
    "VOLUME",
    "USER",
    "WORKDIR",
    "ARG",
    "ONBUILD",
    "STOPSIGNAL",
    "HEALTHCHECK",
    "SHELL",
}


def validate_dockerfile_syntax(content_or_path: str | Path) -> ValidationResult:
    """Validates Dockerfile syntax, instruction ordering, and base images."""
    if isinstance(content_or_path, Path) or (
        isinstance(content_or_path, str) and "\n" not in content_or_path and Path(content_or_path).exists()
    ):
        try:
            content = Path(content_or_path).read_text(encoding="utf-8")
        except Exception as e:
            return ValidationResult(
                is_valid=False,
                artifact_type="dockerfile",
                message=f"Failed to read Dockerfile: {e}",
                errors=[str(e)],
            )
    else:
        content = str(content_or_path)

    lines = content.splitlines()
    errors: list[str] = []
    has_from = False
    metadata: dict[str, Any] = {
        "base_images": [],
        "exposed_ports": [],
        "workdirs": [],
        "copy_instructions": [],
        "cmd": None,
        "entrypoint": None,
    }

    line_num = 0
    continuation = False
    current_instruction = ""

    for line in lines:
        line_num += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if continuation:
            current_instruction += " " + stripped
            if not stripped.endswith("\\"):
                continuation = False
            else:
                current_instruction = current_instruction[:-1].strip()
            continue

        if stripped.endswith("\\"):
            continuation = True
            current_instruction = stripped[:-1].strip()
            continue

        current_instruction = stripped

        # Parse instruction keyword
        parts = current_instruction.split(maxsplit=1)
        keyword = parts[0].upper()
        args = parts[1] if len(parts) > 1 else ""

        if keyword not in DOCKERFILE_INSTRUCTIONS:
            errors.append(f"Line {line_num}: Unknown instruction '{keyword}'")
            continue

        if keyword == "FROM":
            has_from = True
            # Handle FROM <image> [AS <stage>]
            from_parts = args.split()
            if from_parts:
                metadata["base_images"].append(from_parts[0])
        elif keyword == "EXPOSE":
            for port_str in args.split():
                clean_port = port_str.split("/")[0]
                if clean_port.isdigit():
                    metadata["exposed_ports"].append(int(clean_port))
        elif keyword == "WORKDIR":
            metadata["workdirs"].append(args.strip())
        elif keyword == "COPY" or keyword == "ADD":
            metadata["copy_instructions"].append(args.strip())
        elif keyword == "CMD":
            metadata["cmd"] = args.strip()
        elif keyword == "ENTRYPOINT":
            metadata["entrypoint"] = args.strip()

    if not has_from:
        errors.append("Dockerfile must contain at least one valid 'FROM' instruction.")

    is_valid = len(errors) == 0
    return ValidationResult(
        is_valid=is_valid,
        artifact_type="dockerfile",
        message="Dockerfile syntax valid."
        if is_valid
        else f"Dockerfile syntax validation failed with {len(errors)} error(s).",
        errors=errors,
        parsed_metadata=metadata,
    )


def validate_compose_schema(content_or_path: str | Path) -> ValidationResult:
    """Validates Docker Compose schema and service definitions."""
    if isinstance(content_or_path, Path) or (
        isinstance(content_or_path, str) and "\n" not in content_or_path and Path(content_or_path).exists()
    ):
        try:
            content = Path(content_or_path).read_text(encoding="utf-8")
        except Exception as e:
            return ValidationResult(
                is_valid=False,
                artifact_type="compose",
                message=f"Failed to read Compose file: {e}",
                errors=[str(e)],
            )
    else:
        content = str(content_or_path)

    try:
        data = yaml.safe_load(content)
    except Exception as e:
        return ValidationResult(
            is_valid=False,
            artifact_type="compose",
            message=f"Compose YAML parsing error: {e}",
            errors=[str(e)],
        )

    if not isinstance(data, dict):
        return ValidationResult(
            is_valid=False,
            artifact_type="compose",
            message="Compose root must be a YAML mapping/dictionary.",
            errors=["Root is not a dictionary"],
        )

    errors: list[str] = []
    services = data.get("services")
    if not services or not isinstance(services, dict):
        errors.append("Compose file must contain a non-empty 'services' dictionary.")

    metadata: dict[str, Any] = {"service_names": [], "services": {}}

    if isinstance(services, dict):
        for svc_name, svc_cfg in services.items():
            metadata["service_names"].append(svc_name)
            if not isinstance(svc_cfg, dict):
                errors.append(f"Service '{svc_name}' configuration must be a dictionary.")
                continue

            has_build = "build" in svc_cfg
            has_image = "image" in svc_cfg
            if not has_build and not has_image:
                errors.append(f"Service '{svc_name}' must specify either 'build' or 'image'.")

            metadata["services"][svc_name] = {
                "has_build": has_build,
                "has_image": has_image,
                "ports": svc_cfg.get("ports", []),
                "build": svc_cfg.get("build"),
            }

    is_valid = len(errors) == 0
    return ValidationResult(
        is_valid=is_valid,
        artifact_type="compose",
        message="Compose schema valid."
        if is_valid
        else f"Compose schema validation failed with {len(errors)} error(s).",
        errors=errors,
        parsed_metadata=metadata,
    )
