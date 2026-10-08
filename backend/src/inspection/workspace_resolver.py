"""Workspace graph analyzer and deterministic ambiguity resolver.

Determines the authoritative application root, build commands, and runtime configuration
across single applications, nested directories, and monorepos without hardcoded path heuristics.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.src.blueprint.models import (
    AmbiguityResolution,
    BuildConfig,
    NetworkContract,
    ProjectBlueprint,
    ProtocolType,
    RuntimeContract,
    WorkloadType,
)
from backend.src.inspection.scanner import DiscoveredFile, DiscoveredRepository


def _read_json_safe(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_text_safe(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


class WorkspacePackageInfo:
    """Parsed metadata for a package/module within the repository."""
    def __init__(self, manifest: DiscoveredFile):
        self.manifest = manifest
        self.directory = manifest.directory
        self.name = manifest.directory
        self.is_runnable = False
        self.is_library = False
        self.framework: Optional[str] = None
        self.package_manager = "npm"
        self.start_command = ""
        self.build_command: Optional[str] = None
        self.install_command: Optional[str] = None
        self.output_dir: Optional[str] = None
        self.listen_port: Optional[int] = 3000
        self.workload_type = WorkloadType.WEB_SERVICE
        self.dependencies: List[str] = []
        self.is_workspace_root = False
        self.declared_workspaces: List[str] = []


def _inspect_node_package(pkg_path: Path, manifest: DiscoveredFile, repo_lockfiles: List[DiscoveredFile]) -> WorkspacePackageInfo:
    info = WorkspacePackageInfo(manifest)
    data = _read_json_safe(pkg_path) or {}
    info.name = data.get("name", manifest.directory)
    scripts = data.get("scripts", {})
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    info.dependencies = list(deps.keys())

    ws = data.get("workspaces")
    if ws:
        info.is_workspace_root = True
        if isinstance(ws, list):
            info.declared_workspaces = [w.rstrip("/*") for w in ws]
        elif isinstance(ws, dict) and "packages" in ws:
            info.declared_workspaces = [w.rstrip("/*") for w in ws["packages"]]

    # Detect package manager
    lock_names = {lf.filename for lf in repo_lockfiles}
    if "pnpm-lock.yaml" in lock_names:
        info.package_manager = "pnpm"
    elif "yarn.lock" in lock_names:
        info.package_manager = "yarn"
    elif "bun.lockb" in lock_names:
        info.package_manager = "bun"
    else:
        info.package_manager = "npm"

    # Detect framework
    if "next" in deps:
        info.framework = "nextjs"
        info.workload_type = WorkloadType.WEB_SERVICE
        info.output_dir = ".next"
        info.listen_port = 3000
    elif "nuxt" in deps or "nuxt3" in deps:
        info.framework = "nuxtjs"
        info.workload_type = WorkloadType.WEB_SERVICE
        info.output_dir = ".output"
        info.listen_port = 3000
    elif "vite" in deps or "react-scripts" in deps:
        if "express" not in deps and "fastify" not in deps:
            info.framework = "vite" if "vite" in deps else "create-react-app"
            info.workload_type = WorkloadType.STATIC_SPA
            info.output_dir = "dist" if "vite" in deps else "build"
            info.listen_port = 80
        else:
            info.framework = "node-web"
            info.workload_type = WorkloadType.WEB_SERVICE
            info.listen_port = 3000
    elif "express" in deps or "fastify" in deps or "nest" in deps or "@nestjs/core" in deps:
        info.framework = "nestjs" if "@nestjs/core" in deps else ("fastify" if "fastify" in deps else "express")
        info.workload_type = WorkloadType.WEB_SERVICE
        info.listen_port = 3000
    else:
        info.framework = "nodejs-generic"
        info.listen_port = 3000

    # Build and start command resolution
    if "build" in scripts:
        info.build_command = f"{info.package_manager} run build"
    if "start" in scripts:
        info.start_command = f"{info.package_manager} run start"
        info.is_runnable = True
    elif "serve" in scripts:
        info.start_command = f"{info.package_manager} run serve"
        info.is_runnable = True
    elif data.get("main"):
        main_entry = data.get("main")
        info.start_command = f"node {main_entry}"
        info.is_runnable = True

    # Check if this package is purely a library
    if not info.is_runnable and not info.framework:
        info.is_library = True

    return info


def _inspect_python_package(manifest: DiscoveredFile, repo_lockfiles: List[DiscoveredFile]) -> WorkspacePackageInfo:
    info = WorkspacePackageInfo(manifest)
    info.package_manager = "pip"
    content = _read_text_safe(Path(manifest.absolute_path))

    # Lockfile detection
    lock_names = {lf.filename for lf in repo_lockfiles}
    if "poetry.lock" in lock_names or "tool.poetry" in content:
        info.package_manager = "poetry"
    elif "uv.lock" in lock_names:
        info.package_manager = "uv"
    elif "Pipfile.lock" in lock_names or manifest.filename == "Pipfile":
        info.package_manager = "pipenv"

    if "fastapi" in content or "uvicorn" in content:
        info.framework = "fastapi"
        info.workload_type = WorkloadType.WEB_SERVICE
        info.start_command = "uvicorn main:app --host 0.0.0.0 --port 8000"
        info.listen_port = 8000
        info.is_runnable = True
    elif "flask" in content:
        info.framework = "flask"
        info.workload_type = WorkloadType.WEB_SERVICE
        info.start_command = "flask run --host=0.0.0.0 --port=5000"
        info.listen_port = 5000
        info.is_runnable = True
    elif "django" in content:
        info.framework = "django"
        info.workload_type = WorkloadType.WEB_SERVICE
        info.start_command = "python manage.py runserver 0.0.0.0:8000"
        info.listen_port = 8000
        info.is_runnable = True
    elif "celery" in content:
        info.framework = "celery"
        info.workload_type = WorkloadType.BACKGROUND_WORKER
        info.start_command = "celery -A tasks worker --loglevel=info"
        info.listen_port = None
        info.is_runnable = True
    else:
        info.framework = "python-generic"
        info.start_command = "python main.py"
        info.listen_port = 8000
        info.is_runnable = True

    return info


def _inspect_go_package(manifest: DiscoveredFile) -> WorkspacePackageInfo:
    info = WorkspacePackageInfo(manifest)
    info.package_manager = "go"
    info.framework = "golang"
    info.workload_type = WorkloadType.WEB_SERVICE
    info.build_command = "go build -o server ."
    info.start_command = "./server"
    info.listen_port = 8080
    info.is_runnable = True
    return info


def _inspect_rust_package(manifest: DiscoveredFile) -> WorkspacePackageInfo:
    info = WorkspacePackageInfo(manifest)
    info.package_manager = "cargo"
    info.framework = "rust"
    info.workload_type = WorkloadType.WEB_SERVICE
    info.build_command = "cargo build --release"
    info.start_command = "./target/release/app"
    info.listen_port = 8080
    info.is_runnable = True
    return info


def resolve_repository_blueprint(
    discovered: DiscoveredRepository,
    blueprint_id: str = "bp-auto",
    requested_target: Optional[str] = None,
) -> ProjectBlueprint:
    """Deterministically resolves the repository into an authoritative ProjectBlueprint."""
    packages: List[WorkspacePackageInfo] = []

    # 1. Parse all discovered manifests
    for manifest in discovered.manifests:
        if manifest.filename == "package.json":
            packages.append(_inspect_node_package(Path(manifest.absolute_path), manifest, discovered.lockfiles))
        elif manifest.filename in {"pyproject.toml", "requirements.txt", "Pipfile", "setup.py"}:
            packages.append(_inspect_python_package(manifest, discovered.lockfiles))
        elif manifest.filename == "go.mod":
            packages.append(_inspect_go_package(manifest))
        elif manifest.filename == "Cargo.toml":
            packages.append(_inspect_rust_package(manifest))

    is_monorepo = len(packages) > 1 or discovered.has_manifest("pnpm-workspace.yaml")

    # 2. Filter candidates
    runnable_candidates = [p for p in packages if p.is_runnable]

    selected_package: Optional[WorkspacePackageInfo] = None
    ambiguity = AmbiguityResolution(is_ambiguous=False, confidence_score=1.0)

    # If operator explicitly requested a target directory or name
    if requested_target:
        matches = [p for p in packages if p.directory == requested_target or p.name == requested_target]
        if matches:
            selected_package = matches[0]
            ambiguity.resolution_strategy = "operator_specified"

    # Check for monorepo workspace root with declared workspace members
    workspace_roots = [p for p in packages if p.is_workspace_root]
    if not selected_package and workspace_roots:
        root_ws = workspace_roots[0]
        declared_candidates = [
            p for p in packages
            if not p.is_workspace_root and any(p.directory == dw or p.name == dw or p.directory.startswith(dw) for dw in root_ws.declared_workspaces)
        ]
        if len(declared_candidates) == 1:
            selected_package = declared_candidates[0]
            ambiguity.resolution_strategy = "workspace_root_declared_target"
            ambiguity.confidence_score = 0.98
        elif len(declared_candidates) > 1:
            runnable_candidates = [p for p in declared_candidates if p.is_runnable]

    # Single package detected (root or nested subfolder)
    if not selected_package and len(packages) == 1:
        selected_package = packages[0]
        ambiguity.resolution_strategy = "single_package"
        ambiguity.confidence_score = 1.0

    # Multiple packages: deterministic resolution
    if not selected_package and packages:
        if len(runnable_candidates) == 1:
            # Exactly one runnable application target
            selected_package = runnable_candidates[0]
            ambiguity.resolution_strategy = "single_runnable_candidate"
            ambiguity.confidence_score = 0.95
        elif len(runnable_candidates) > 1:
            # Check if one is a web frontend/gateway and others are non-web or internal
            web_candidates = [p for p in runnable_candidates if p.workload_type == WorkloadType.WEB_SERVICE]
            if len(web_candidates) == 1:
                selected_package = web_candidates[0]
                ambiguity.resolution_strategy = "single_web_service_priority"
                ambiguity.confidence_score = 0.90
            else:
                # Ambiguity cannot be deterministically resolved without operator choice
                candidate_dirs = [p.directory for p in runnable_candidates]
                ambiguity.is_ambiguous = True
                ambiguity.confidence_score = 0.40
                ambiguity.detected_candidates = candidate_dirs
                ambiguity.unresolved_reason = (
                    f"Multiple runnable applications detected ({', '.join(candidate_dirs)}). "
                    "Operator clarification required to designate primary deployment target."
                )
                selected_package = runnable_candidates[0]  # Provisional fallback

    # Fallback default if no packages found
    if not selected_package:
        selected_package = WorkspacePackageInfo(
            DiscoveredFile(relative_path=".", absolute_path=discovered.root_path, filename="", directory=".", depth=0)
        )
        selected_package.start_command = "echo 'No entrypoint detected'"
        ambiguity.is_ambiguous = True
        ambiguity.confidence_score = 0.1
        ambiguity.unresolved_reason = "No ecosystem manifest discovered in repository."

    # Language determination
    lang = "nodejs"
    if selected_package.package_manager in {"poetry", "pip", "uv", "pipenv"}:
        lang = "python"
    elif selected_package.package_manager == "go":
        lang = "golang"
    elif selected_package.package_manager == "cargo":
        lang = "rust"

    # Runtime version hint discovery
    version = "20" if lang == "nodejs" else ("3.11" if lang == "python" else "latest")
    for rh in discovered.runtime_hints:
        if lang == "nodejs" and rh.filename in {".nvmrc", ".node-version"}:
            ver_text = _read_text_safe(Path(rh.absolute_path)).strip()
            if ver_text:
                version = ver_text.lstrip("v")
        elif lang == "python" and rh.filename in {".python-version", "runtime.txt"}:
            ver_text = _read_text_safe(Path(rh.absolute_path)).strip()
            if ver_text:
                version = ver_text.replace("python-", "")

    # Network contract
    proto = ProtocolType.HTTP if selected_package.workload_type in {WorkloadType.WEB_SERVICE, WorkloadType.STATIC_SPA} else (
        ProtocolType.TCP if selected_package.workload_type == WorkloadType.TCP_SERVICE else ProtocolType.NONE
    )

    network = NetworkContract(
        listen_port=selected_package.listen_port,
        protocol=proto,
        health_check_path="/health" if proto == ProtocolType.HTTP else None,
        exposed_endpoints=["/"] if proto == ProtocolType.HTTP else [],
    )

    build_cfg = BuildConfig(
        source_dir=selected_package.directory,
        build_command=selected_package.build_command,
        artifact_output_dir=selected_package.output_dir,
        install_command=f"{selected_package.package_manager} install",
    )

    runtime_contract = RuntimeContract(
        language=lang,
        runtime_version=version,
        framework=selected_package.framework,
        package_manager=selected_package.package_manager,
        start_command=selected_package.start_command,
    )

    return ProjectBlueprint(
        blueprint_id=blueprint_id,
        repository_root=discovered.root_path,
        is_monorepo=is_monorepo,
        workload_type=selected_package.workload_type,
        build_config=build_cfg,
        runtime=runtime_contract,
        network=network,
        ambiguity=ambiguity,
    )
