"""G3 Pre-Execution Consistency Gate for ForgeOps.

Statically asserts that generated or selected deployment manifests are consistent
with the physical filesystem and the authoritative ProjectBlueprint before container execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    from src.blueprint.models import ProjectBlueprint
except ImportError:
    from backend.src.blueprint.models import ProjectBlueprint


@dataclass
class ConsistencyGateResult:
    """Outcome of the G3 Pre-Execution Consistency Gate."""

    gate_id: str = "G3"
    passed: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def verify_consistency_gate(
    repo_path: str | Path,
    dockerfile_path: str | Path,
    compose_path: str | Path | None,
    blueprint: ProjectBlueprint,
) -> ConsistencyGateResult:
    """Performs static assertion of build context, COPY targets, and port alignment."""
    root = Path(repo_path).resolve()
    df_path = Path(dockerfile_path).resolve() if not Path(dockerfile_path).is_absolute() else Path(dockerfile_path)
    errors: list[str] = []

    # 1. Assert Dockerfile exists
    if not df_path.exists():
        errors.append(f"Dockerfile not found at specified path: {df_path}")
        return ConsistencyGateResult(gate_id="G3", passed=False, message="Dockerfile missing", errors=errors)

    # 2. Determine build context
    build_context = root
    if compose_path:
        cp_path = Path(compose_path).resolve() if not Path(compose_path).is_absolute() else Path(compose_path)
        if cp_path.exists():
            try:
                with open(cp_path, encoding="utf-8") as f:
                    compose_data = yaml.safe_load(f) or {}
                services = compose_data.get("services", {})
                for _svc_name, svc_cfg in services.items():
                    build_info = svc_cfg.get("build")
                    if isinstance(build_info, str):
                        build_context = (cp_path.parent / build_info).resolve()
                        break
                    elif isinstance(build_info, dict) and "context" in build_info:
                        build_context = (cp_path.parent / build_info["context"]).resolve()
                        break
            except Exception as e:
                errors.append(f"Failed to parse Compose file for build context: {e}")

    if not build_context.exists() or not build_context.is_dir():
        errors.append(f"Build context directory does not exist: {build_context}")

    # 3. Parse COPY and ADD instructions in Dockerfile
    try:
        content = df_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("COPY") or stripped.startswith("ADD"):
                parts = stripped.split()
                # Filter out flags like --from=builder, --chown=...
                src_parts = [p for p in parts[1:-1] if not p.startswith("--")]
                # If instruction copies from a previous stage (--from=...), skip host filesystem check
                if any(p.startswith("--from=") for p in parts[1:]):
                    continue

                for src in src_parts:
                    # Ignore glob patterns for simple static existence check
                    if "*" in src or "?" in src:
                        continue
                    candidate = (build_context / src).resolve()
                    if not candidate.exists():
                        errors.append(
                            f"Line {idx}: Referenced path '{src}' does not exist in build context '{build_context}'"
                        )

        # Check workspace manifest copying before RUN install
        root_pkg = root / "package.json"
        is_workspace = False
        workspace_dirs: list[str] = []
        if root_pkg.exists():
            try:
                import json

                pkg_data = json.loads(root_pkg.read_text(encoding="utf-8"))
                ws_entry = pkg_data.get("workspaces")
                if isinstance(ws_entry, list):
                    workspace_dirs.extend([w for w in ws_entry if isinstance(w, str) and "*" not in w])
                    is_workspace = len(ws_entry) > 0
                elif isinstance(ws_entry, dict) and "packages" in ws_entry:
                    workspace_dirs.extend([w for w in ws_entry["packages"] if isinstance(w, str) and "*" not in w])
                    is_workspace = len(ws_entry["packages"]) > 0
            except Exception:
                pass

        pnpm_ws = root / "pnpm-workspace.yaml"
        if pnpm_ws.exists():
            is_workspace = True

        if is_workspace and workspace_dirs:
            install_seen = False
            copied_workspaces = False
            for line in lines:
                s = line.strip()
                has_install_cmd = any(cmd in s for cmd in ["npm install", "npm ci", "pnpm install", "yarn install"])
                if s.startswith("RUN ") and has_install_cmd:
                    install_seen = True
                    break
                if s.startswith("COPY "):
                    if any(ws in s for ws in workspace_dirs) or s.startswith("COPY . .") or s.startswith("COPY . /"):
                        copied_workspaces = True

            if install_seen and not copied_workspaces:
                errors.append(
                    "Workspace repository runs dependency installation before copying workspace package manifests. "
                    "Workspace manifests must be copied before RUN install to prevent missing executables "
                    "(exit code 127)."
                )

        # Multi-stage SSR check: if Next.js/SSR and not standalone, check for invalid /app/dist copies
        framework_lower = ""
        if hasattr(blueprint, "runtime") and blueprint.runtime and blueprint.runtime.framework:
            framework_lower = getattr(blueprint.runtime, "framework", "").lower()
        if "next" in framework_lower or "nuxt" in framework_lower:
            for idx, line in enumerate(lines, 1):
                s = line.strip()
                if s.startswith("COPY ") and "--from=" in s and ("/app/dist" in s or "/dist" in s):
                    errors.append(
                        f"Line {idx}: Framework '{framework_lower}' does not output to dist. "
                        "Multi-stage Dockerfile must copy full application tree or use standalone output."
                    )
    except Exception as e:
        errors.append(f"Failed to inspect Dockerfile instructions: {e}")

    # 4. Port alignment check
    if blueprint.network.listen_port:
        target_port = blueprint.network.listen_port
        exposed_in_df = f"EXPOSE {target_port}" in content or "EXPOSE" not in content  # Non-blocking if EXPOSE omitted
        if not exposed_in_df:
            errors.append(f"Dockerfile EXPOSE directive does not include blueprint listen_port {target_port}")

    passed = len(errors) == 0
    message = (
        "Pre-execution consistency verified." if passed else f"G3 Consistency Gate failed with {len(errors)} error(s)."
    )

    return ConsistencyGateResult(
        gate_id="G3",
        passed=passed,
        message=message,
        errors=errors,
        details={
            "build_context": str(build_context),
            "dockerfile": str(df_path),
            "blueprint_id": blueprint.blueprint_id,
        },
    )
