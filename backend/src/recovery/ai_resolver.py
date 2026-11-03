"""Bounded AI Recovery Engine with strict application-source protection.

Resolves deployment failures strictly across manifests and blueprint parameters,
banning any modification of user source code and bounding attempts to 1-3 iterations.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _detect_npm_workspaces(workspace_path: Path | None) -> list[str]:
    """Detects declared npm workspace package directories or subpackages."""
    if not workspace_path or not workspace_path.exists():
        return []
    workspace_dirs: list[str] = []
    root_pkg = workspace_path / "package.json"
    if root_pkg.exists():
        try:
            pkg_data = json.loads(root_pkg.read_text(encoding="utf-8", errors="replace"))
            ws_entry = pkg_data.get("workspaces")
            if isinstance(ws_entry, list):
                for w in ws_entry:
                    if isinstance(w, str) and "*" not in w:
                        if (workspace_path / w / "package.json").exists():
                            workspace_dirs.append(w)
            elif isinstance(ws_entry, dict) and "packages" in ws_entry:
                for w in ws_entry["packages"]:
                    if isinstance(w, str) and "*" not in w:
                        if (workspace_path / w / "package.json").exists():
                            workspace_dirs.append(w)
        except Exception:
            pass

    if not workspace_dirs:
        try:
            for child in workspace_path.iterdir():
                if child.is_dir() and not child.name.startswith((".", "node_modules", "dist", "build", "target")):
                    if (child / "package.json").exists():
                        workspace_dirs.append(child.name)
        except Exception:
            pass

    return sorted(list(set(workspace_dirs)))


try:
    from src.blueprint.models import ProjectBlueprint
    from src.diagnostics.bundle import DiagnosticBundle
    from src.generation.artifact_checks import validate_artifacts
    from src.generation.model_prompt import parse_artifacts
    from src.secrets.redaction import create_redacted_prompt
except ImportError:
    from backend.src.blueprint.models import ProjectBlueprint
    from backend.src.diagnostics.bundle import DiagnosticBundle
    from backend.src.generation.artifact_checks import validate_artifacts
    from backend.src.generation.model_prompt import parse_artifacts
    from backend.src.secrets.redaction import create_redacted_prompt


@dataclass
class _FileArtifact:
    """Artifact adapter with path and content for validate_artifacts."""

    path: str
    content: str


@dataclass
class RecoveryPlan:
    """Actionable recovery plan produced by AI resolution."""

    status: str  # "PROPOSED", "HALTED_MAX_ITERATIONS", "HALTED_APPLICATION_CODE_DEFECT", "HALTED_INFRASTRUCTURE"
    iteration: int
    root_cause_category: str  # "BLUEPRINT_MISMATCH", "ARTIFACT_DEFECT", "APPLICATION_CODE", "INFRASTRUCTURE"
    operator_message: str
    suggested_manifests: dict[str, str] = field(default_factory=dict)
    updated_blueprint: ProjectBlueprint | None = None


def resolve_failure_with_ai(
    bundle: DiagnosticBundle,
    current_iteration: int,
    workspace_path: Path | None = None,
) -> RecoveryPlan:
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

    # 3. Classify defect categories accurately from diagnostic evidence
    stderr_lower = (bundle.stderr + "\n" + bundle.stdout).lower()
    container_state = bundle.container_state or {}
    c_running = container_state.get("Running", False) if isinstance(container_state, dict) else False
    if not c_running and isinstance(container_state, dict):
        c_running = container_state.get("container_running", False)
    exit_code = container_state.get("ExitCode") if isinstance(container_state, dict) else None

    root_cause = "ARTIFACT_DEFECT"
    suggested_manifests: dict[str, str] = {}
    updated_bp: ProjectBlueprint | None = None

    if "bind: address already in use" in stderr_lower or "port collision" in stderr_lower:
        root_cause = "INFRASTRUCTURE"
        return RecoveryPlan(
            status="HALTED_INFRASTRUCTURE",
            iteration=current_iteration,
            root_cause_category="INFRASTRUCTURE",
            operator_message="Host port collision detected. Resolve conflicting container or assign alternative port.",
        )
    elif (
        "connection attempts failed" in stderr_lower
        or "connection refused" in stderr_lower
        or "refused connection" in stderr_lower
        or "port_connection_refused" in stderr_lower
        or "loopback_binding_detected" in stderr_lower
        or "workload readiness probe failed" in stderr_lower
        or "application process exited" in stderr_lower
        or "workload container failed to stay running" in stderr_lower
        or "http 403" in stderr_lower
        or "forbidden" in stderr_lower
    ):
        if c_running:
            if "http 403" in stderr_lower or "forbidden" in stderr_lower:
                root_cause = "ENDPOINT_ACCESS_DEFECT"
                operator_msg = (
                    "Workload container is running and responding, but rejected probe with HTTP 403 Forbidden. "
                    "Host header validation or route access permissions require adjustment; "
                    "existing deployment manifests are valid and will not be regenerated."
                )
                return RecoveryPlan(
                    status="PROPOSED",
                    iteration=current_iteration,
                    root_cause_category=root_cause,
                    operator_message=operator_msg,
                    suggested_manifests={},
                    updated_blueprint=None,
                )
            root_cause = "NETWORK_ENDPOINT_DEFECT"
            operator_msg = (
                f"Workload container is running but verification probe failed ({bundle.stderr or 'Connection refused'}). "
                "Verifier network namespace, container listening port, or endpoint routing requires adjustment; "
                "existing deployment manifests are valid and will not be regenerated."
            )
            # Do NOT regenerate Dockerfile for network endpoint defects
            return RecoveryPlan(
                status="PROPOSED",
                iteration=current_iteration,
                root_cause_category=root_cause,
                operator_message=operator_msg,
                suggested_manifests={},
                updated_blueprint=None,
            )
        else:
            root_cause = "RUNTIME_STARTUP_DEFECT"
            ec_str = f"exit code {exit_code}" if exit_code is not None else "crashed process"
            operator_msg = (
                f"Workload container failed to stay running ({ec_str}). "
                f"Application process exited before healthcheck converged. Diagnostic: {bundle.stderr or bundle.stdout}"
            )
            return RecoveryPlan(
                status="HALTED_RUNTIME_DEFECT",
                iteration=current_iteration,
                root_cause_category=root_cause,
                operator_message=operator_msg,
                suggested_manifests={},
                updated_blueprint=None,
            )
    elif "missing required github_config" in stderr_lower or "missing required vercel_config" in stderr_lower:
        root_cause = "READINESS_CONFIGURATION_DEFECT"
        return RecoveryPlan(
            status="HALTED_CONFIGURATION_DEFECT",
            iteration=current_iteration,
            root_cause_category=root_cause,
            operator_message="Required strategy configuration is missing. Provide required repository or target parameters.",
        )
    ws_dirs = _detect_npm_workspaces(workspace_path)
    ws_copies = []
    for w in ws_dirs:
        if " " in w:
            ws_copies.append(f'COPY ["{w}/package*.json", "./{w}/"]')
        else:
            ws_copies.append(f"COPY {w}/package*.json ./{w}/")
    ws_copy_block = "".join(f"{c}\n" for c in ws_copies)

    if "file not found in build context" in stderr_lower or "copy failed" in stderr_lower:
        root_cause = "BLUEPRINT_MISMATCH"
        operator_msg = "Deterministic recovery template applied: identified mismatch between build context and Dockerfile COPY instructions. Correcting manifest paths."
        suggested_manifests["Dockerfile"] = (
            "# Multi-stage Dockerfile generated by ForgeOps AI Recovery\n"
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            f"{ws_copy_block}"
            "RUN npm ci || npm install\n"
            "COPY . .\n"
            "RUN npm run build --if-present\n\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "ENV NODE_ENV=production\n"
            "COPY --from=builder --chown=node:node /app ./\n"
            "USER node\n"
            "EXPOSE 3000\n"
            'CMD ["npm", "start"]\n'
        )
    elif (
        "missing required dockerfile" in stderr_lower
        or "dockerfile not found" in stderr_lower
        or ("image" in stderr_lower and "not found in docker daemon" in stderr_lower)
    ):
        root_cause = "ARTIFACT_DEFECT"
        suggested_manifests["Dockerfile"] = (
            "# Multi-stage Dockerfile generated by ForgeOps AI Recovery\n"
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            f"{ws_copy_block}"
            "RUN npm ci || npm install\n"
            "COPY . .\n"
            "RUN npm run build\n\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "ENV NODE_ENV=production\n"
            "COPY --from=builder --chown=node:node /app ./\n"
            "USER node\n"
            "EXPOSE 3000\n"
            'CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "3000"]\n'
        )
        suggested_manifests[".dockerignore"] = ".git\nnode_modules\n.venv\n__pycache__\n.forgeops\n.env*.local\ndist\n"
        operator_msg = "Deterministic recovery template applied: synthesized missing multi-stage Dockerfile and .dockerignore for container build."
    elif "syntax error" in stderr_lower and "dockerfile" in stderr_lower:
        root_cause = "ARTIFACT_DEFECT"
        operator_msg = "Deterministic recovery template applied: synthesized corrected Dockerfile syntax following compilation error."
        suggested_manifests["Dockerfile"] = (
            "# Multi-stage Dockerfile generated by ForgeOps AI Recovery\n"
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            f"{ws_copy_block}"
            "RUN npm ci || npm install\n"
            "COPY . .\n"
            "RUN npm run build --if-present\n\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "ENV NODE_ENV=production\n"
            "COPY --from=builder --chown=node:node /app ./\n"
            "USER node\n"
            "EXPOSE 3000\n"
            'CMD ["npm", "start"]\n'
        )
    else:
        root_cause = "ARTIFACT_DEFECT"
        operator_msg = (
            f"Deterministic recovery template applied: analyzed build/apply failure in gate {bundle.gate_identifier} "
            "and synthesized recovery manifests."
        )
        suggested_manifests["Dockerfile"] = (
            "# Multi-stage Dockerfile generated by ForgeOps AI Recovery\n"
            "FROM node:20-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            f"{ws_copy_block}"
            "RUN npm ci || npm install\n"
            "COPY . .\n"
            "RUN npm run build --if-present\n\n"
            "FROM node:20-alpine AS runner\n"
            "WORKDIR /app\n"
            "ENV NODE_ENV=production\n"
            "COPY --from=builder --chown=node:node /app ./\n"
            "USER node\n"
            "EXPOSE 3000\n"
            'CMD ["npm", "start"]\n'
        )
        suggested_manifests[".dockerignore"] = "node_modules\n.next\n.git\n.forgeops\nnpm-debug.log*\n"

    return RecoveryPlan(
        status="PROPOSED",
        iteration=current_iteration,
        root_cause_category=root_cause,
        operator_message=operator_msg,
        suggested_manifests=suggested_manifests,
        updated_blueprint=updated_bp,
    )


async def resolve_failure_with_ai_async(
    bundle: DiagnosticBundle,
    current_iteration: int,
    workspace_path: Path | None = None,
    session: Any = None,
    run: Any = None,
    context: Any = None,
    base_url: str | None = None,
    model: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> RecoveryPlan:
    """Analyzes a DiagnosticBundle asynchronously, consulting self-hosted Ollama model when possible."""
    # First, run the deterministic analysis to catch halts and non-manifest issues
    sync_plan = resolve_failure_with_ai(bundle, current_iteration=current_iteration, workspace_path=workspace_path)

    # If the sync plan halted, or determined this is not an artifact defect that needs model repair, return it
    if sync_plan.status != "PROPOSED":
        return sync_plan

    if sync_plan.root_cause_category in ("ENDPOINT_ACCESS_DEFECT", "NETWORK_ENDPOINT_DEFECT"):
        return sync_plan

    # Check if this is a mock / simulation environment without a custom client
    is_mock = False
    if client is None:
        if (
            type(session).__name__ == "MockAsyncSession"
            or "Mock" in type(session).__name__
            or getattr(session, "_is_mock", False)
            or getattr(context, "simulation_mode", False)
            or (isinstance(context, dict) and context.get("simulation_mode", False))
            or os.getenv("FORGEOPS_SIMULATION_MODE", "").lower() in ("1", "true")
        ):
            is_mock = True

    if is_mock:
        return sync_plan

    # Gather context from workspace if available
    ws_dirs = _detect_npm_workspaces(workspace_path) if workspace_path else []
    ws_context_files: list[str] = []
    if workspace_path and workspace_path.exists():
        check_names = [
            "package.json",
            "requirements.txt",
            "pyproject.toml",
            "pom.xml",
            "build.gradle",
            "Cargo.toml",
            "Dockerfile",
            ".dockerignore",
        ]
        for ws_dir in ws_dirs:
            check_names.append(f"{ws_dir}/package.json")
            for cfg_name in ("next.config.js", "next.config.mjs", "next.config.ts"):
                check_names.append(f"{ws_dir}/{cfg_name}")

        for check_name in check_names:
            p = workspace_path / check_name
            if p.exists() and p.is_file():
                try:
                    content_snippet = p.read_text(encoding="utf-8", errors="replace")[:2000]
                    ws_context_files.append(f"### FILE: {check_name}\n```\n{content_snippet}\n```")
                except Exception:
                    pass

    ws_section = "\n\n".join(ws_context_files) if ws_context_files else "No workspace manifest files found."
    ws_hint = ""
    if ws_dirs:
        copy_examples = []
        for w in ws_dirs:
            if " " in w:
                copy_examples.append(f'COPY ["{w}/package*.json", "./{w}/"]')
            else:
                copy_examples.append(f"COPY {w}/package*.json ./{w}/")
        ws_hint = (
            f"\nRepository architecture note: This repository uses npm workspaces ({', '.join(ws_dirs)}).\n"
            f"All workspace package manifests MUST be copied before running 'npm ci' / 'npm install':\n"
            + "\n".join(copy_examples)
            + "\n"
        )

    strat = ""
    if run is not None:
        strat = str(
            getattr(run, "strategy", None)
            or (run.configuration or {}).get("strategy")
            or (run.configuration or {}).get("deployment_strategy")
            or ""
        ).lower()
    is_docker_only = strat in ("docker_only", "docker") or "docker_only" in strat

    rules_lines = [
        "1. Never modify application source code.",
        "2. For Dockerfile, ensure the first instruction is FROM (or ARG then FROM). Dropping root is mandatory: place a line that drops root with USER (such as `USER node` or `USER 10001`) after build/install steps and before CMD/ENTRYPOINT.",
        "3. Multi-stage build and absolute paths: Use multi-stage builds. When copying from the builder stage, always use absolute paths from builder WORKDIR (e.g. `COPY --from=builder /app ./` or `COPY --from=builder /app/dist ./`). Never use relative paths like `./dist`.",
        '4. Monorepo and workspaces: When workspace packages exist (declared in package.json workspaces), you MUST copy the root manifest, lockfile, AND all workspace package manifests BEFORE executing dependency installation (`npm ci` or `npm install`). If any folder path contains spaces, use Docker JSON array syntax: `COPY ["<path>/package*.json", "./<path>/"]`.',
        '5. Next.js applications: Next.js outputs to `.next` (never `dist`). Multi-stage Dockerfiles must copy the built application tree (e.g. `COPY --from=builder /app ./`) and start with `CMD ["npm", "run", "start"]` or standalone output (`node server.js`). Never use `npx serve -s dist` for Next.js applications.',
        '6. Runtime package manifests: If the final stage executes npm commands (such as CMD ["npm", "start"]), the final stage MUST copy package manifests (e.g. `COPY package*.json ./`) or copy the built workspace (e.g. `COPY --from=builder /app ./` or `COPY --from=builder --chown=node:node /app ./`) so npm does not fail with ENOENT at runtime.',
        "7. Never invent or copy non-existent files: Do NOT copy files like .npmrc, .env, or extra config files unless they are explicitly present in the existing workspace context above.",
    ]
    if is_docker_only:
        rules_lines.append(
            f"{len(rules_lines) + 1}. STRICT STRATEGY REQUIREMENT: This deployment is strictly Docker-only. Do NOT generate Kubernetes manifests, Helm charts, Helm templates, OpenTofu, or cloud manifests. ONLY emit Dockerfile, .dockerignore, or docker-compose.yml."
        )
    rules_lines.append(
        f"{len(rules_lines) + 1}. Emit each manifest using the format:\n### FILE: <relative_path>\n```\n<content>\n```"
    )
    rules_text = "\n".join(rules_lines)

    raw_prompt = (
        f"Deployment stage '{bundle.stage}' (gate '{bundle.gate_identifier}') failed at iteration {current_iteration}.\n"
        f"Command: {bundle.command_line}\n"
        f"Exit code: {bundle.exit_code}\n"
        f"Failure output:\n{bundle.stderr or bundle.stdout or 'Unknown error'}\n\n"
        f"Existing workspace context:\n{ws_section}\n{ws_hint}\n"
        f"Task:\n"
        f"Synthesize repaired deployment manifests (such as Dockerfile and .dockerignore) to fix this failure.\n"
        f"Strict rules:\n"
        f"{rules_text}\n"
    )

    redacted_prompt = create_redacted_prompt(raw_prompt)

    target_base_url = (base_url or os.getenv("SELF_HOSTED_BASE_URL") or "http://localhost:11434/v1").rstrip("/")
    target_model = model or os.getenv("SELF_HOSTED_MODEL_ID") or os.getenv("SELF_HOSTED_MODEL") or "qwen2.5-coder:7b"

    # 4. Bounded warm-up and readiness inspection for Ollama/self-hosted endpoints
    if client is None:
        clean_base = target_base_url.rstrip("/").removesuffix("/v1")
        if "11434" in clean_base or "ollama" in clean_base:
            try:
                from ..core.ollama_readiness import (
                    OllamaClientCancelledError,
                    OllamaModelNotFoundError,
                    OllamaUnavailableError,
                    OllamaWarmupTimeoutError,
                    ensure_ollama_model_ready,
                )
            except ImportError:
                from src.core.ollama_readiness import (
                    OllamaClientCancelledError,
                    OllamaModelNotFoundError,
                    OllamaUnavailableError,
                    OllamaWarmupTimeoutError,
                    ensure_ollama_model_ready,
                )

            hb_mgr = None
            if isinstance(context, dict):
                hb_mgr = context.get("_pipeline_heartbeat")
            hb_cb = hb_mgr.pulse if hb_mgr and hasattr(hb_mgr, "pulse") else None

            try:
                await ensure_ollama_model_ready(
                    clean_base,
                    target_model,
                    warmup_timeout_seconds=360.0,
                    heartbeat_callback=hb_cb,
                )
            except OllamaModelNotFoundError:
                return RecoveryPlan(
                    status="HALTED_MODEL_NOT_FOUND",
                    iteration=current_iteration,
                    root_cause_category="MODEL_NOT_FOUND",
                    operator_message=f"Configured Ollama model '{target_model}' is not installed on server '{clean_base}'.",
                    suggested_manifests={},
                    updated_blueprint=None,
                )
            except OllamaWarmupTimeoutError as exc:
                return RecoveryPlan(
                    status="HALTED_MODEL_LOAD_TIMEOUT",
                    iteration=current_iteration,
                    root_cause_category="MODEL_LOAD_TIMEOUT",
                    operator_message=f"Configured Ollama model '{target_model}' cold start timed out loading weights into memory: {exc}",
                    suggested_manifests={},
                    updated_blueprint=None,
                )
            except OllamaClientCancelledError:
                return RecoveryPlan(
                    status="HALTED_CLIENT_CANCELLED",
                    iteration=current_iteration,
                    root_cause_category="CLIENT_CANCELLED",
                    operator_message=f"Ollama warm-up for model '{target_model}' was cancelled.",
                    suggested_manifests={},
                    updated_blueprint=None,
                )
            except (OllamaUnavailableError, httpx.RequestError) as exc:
                return RecoveryPlan(
                    status="HALTED_MODEL_UNAVAILABLE",
                    iteration=current_iteration,
                    root_cause_category="MODEL_UNAVAILABLE",
                    operator_message=f"Configured Ollama model request to '{clean_base}' failed: {exc}",
                    suggested_manifests={},
                    updated_blueprint=None,
                )

    request_payload = {
        "model": target_model,
        "messages": [
            {
                "role": "system",
                "content": "You are ForgeOps AI Recovery Agent. Generate corrected deployment manifests to repair failed Docker/container builds. Never modify source code.",
            },
            {
                "role": "user",
                "content": str(redacted_prompt),
            },
        ],
        "temperature": 0.0,
    }

    try:
        if client is not None:
            resp = await client.post(f"{target_base_url}/chat/completions", json=request_payload)
            if resp.status_code == 499:
                return RecoveryPlan(
                    status="HALTED_CLIENT_CANCELLED",
                    iteration=current_iteration,
                    root_cause_category="CLIENT_CANCELLED",
                    operator_message="Ollama inference request was closed/cancelled by client (HTTP 499).",
                    suggested_manifests={},
                    updated_blueprint=None,
                )
            resp.raise_for_status()
            resp_data = resp.json()
        else:
            ollama_timeout_s = float(os.getenv("FORGEOPS_OLLAMA_TIMEOUT_SECONDS", "300.0"))
            timeout = httpx.Timeout(ollama_timeout_s, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as http_client:
                resp = await http_client.post(f"{target_base_url}/chat/completions", json=request_payload)
                if resp.status_code == 499:
                    return RecoveryPlan(
                        status="HALTED_CLIENT_CANCELLED",
                        iteration=current_iteration,
                        root_cause_category="CLIENT_CANCELLED",
                        operator_message="Ollama inference request was closed/cancelled by client (HTTP 499).",
                        suggested_manifests={},
                        updated_blueprint=None,
                    )
                resp.raise_for_status()
                resp_data = resp.json()

        choices = resp_data.get("choices")
        if not choices or not isinstance(choices, list):
            logger.warning("Ollama AI recovery response missing completion choices.")
            return RecoveryPlan(
                status="HALTED_MODEL_MALFORMED_OUTPUT",
                iteration=current_iteration,
                root_cause_category="MODEL_MALFORMED_OUTPUT",
                operator_message=f"Configured Ollama model ({target_model}) returned response missing completion choices.",
                suggested_manifests={},
                updated_blueprint=None,
            )

        model_content = choices[0].get("message", {}).get("content", "")
        if not model_content:
            logger.warning("Ollama AI recovery response returned empty message content.")
            return RecoveryPlan(
                status="HALTED_MODEL_MALFORMED_OUTPUT",
                iteration=current_iteration,
                root_cause_category="MODEL_MALFORMED_OUTPUT",
                operator_message=f"Configured Ollama model ({target_model}) returned empty message content.",
                suggested_manifests={},
                updated_blueprint=None,
            )

        try:
            parsed = parse_artifacts(
                model_content,
                required=(),
                requested=["Dockerfile", ".dockerignore", "dockerignore", "docker-compose.yml", "compose.yaml"],
            )
        except Exception as parse_exc:
            logger.warning(
                "Ollama AI recovery output could not be parsed into manifests: %s (%s)", model_content[:200], parse_exc
            )
            return RecoveryPlan(
                status="HALTED_MODEL_MALFORMED_OUTPUT",
                iteration=current_iteration,
                root_cause_category="MODEL_MALFORMED_OUTPUT",
                operator_message=f"Configured Ollama model ({target_model}) output could not be parsed into actionable deployment manifests: {parse_exc}",
                suggested_manifests={},
                updated_blueprint=None,
            )

        if not parsed:
            logger.warning("Ollama AI recovery output could not be parsed into manifests: %s", model_content[:200])
            return RecoveryPlan(
                status="HALTED_MODEL_MALFORMED_OUTPUT",
                iteration=current_iteration,
                root_cause_category="MODEL_MALFORMED_OUTPUT",
                operator_message=f"Configured Ollama model ({target_model}) output could not be parsed into actionable deployment manifests.",
                suggested_manifests={},
                updated_blueprint=None,
            )

        if "dockerignore" in parsed and ".dockerignore" not in parsed:
            parsed[".dockerignore"] = parsed.pop("dockerignore")

        if is_docker_only:
            parsed = {
                k: v
                for k, v in parsed.items()
                if not (
                    k.startswith(("k8s/", "kubernetes/", "charts/", "helm/", "infra/", ".github/"))
                    or k in ("Chart.yaml", "values.yaml")
                )
            }

        # If workspace directories exist and Dockerfile was produced, ensure workspace manifests are copied before install
        if "Dockerfile" in parsed and ws_dirs:
            df_content = parsed["Dockerfile"]
            df_lines = df_content.splitlines()
            install_line_idx = -1
            ws_copied_before_install = {w: False for w in ws_dirs}
            for idx, line in enumerate(df_lines):
                s = line.strip()
                if s.startswith("RUN ") and any(
                    cmd in s for cmd in ["npm ci", "npm install", "yarn install", "pnpm install"]
                ):
                    if install_line_idx == -1:
                        install_line_idx = idx
                if s.startswith("COPY ") and not s.startswith("COPY --from="):
                    if s.startswith("COPY . .") or s.startswith("COPY . /"):
                        if install_line_idx == -1:
                            for w in ws_dirs:
                                ws_copied_before_install[w] = True
                    for w in ws_dirs:
                        if (
                            (w in s or f'"{w}' in s)
                            and "package" in s.lower()
                            and (install_line_idx == -1 or idx < install_line_idx)
                        ):
                            ws_copied_before_install[w] = True

            needs_reordering = (install_line_idx != -1) and not all(ws_copied_before_install.values())
            if needs_reordering:
                filtered_lines: list[str] = []
                for _idx, line in enumerate(df_lines):
                    s = line.strip()
                    if (
                        s.startswith("COPY ")
                        and not s.startswith("COPY --from=")
                        and any(f"{w}/package" in s for w in ws_dirs)
                    ):
                        continue
                    filtered_lines.append(line)

                new_install_idx = -1
                for idx, line in enumerate(filtered_lines):
                    s = line.strip()
                    if s.startswith("RUN ") and any(
                        cmd in s for cmd in ["npm ci", "npm install", "yarn install", "pnpm install"]
                    ):
                        new_install_idx = idx
                        break

                ws_copies = [
                    f'COPY ["{w}/package*.json", "./{w}/"]' if " " in w else f"COPY {w}/package*.json ./{w}/"
                    for w in ws_dirs
                ]
                new_lines: list[str] = []
                for idx, line in enumerate(filtered_lines):
                    if idx == new_install_idx:
                        for wc in ws_copies:
                            new_lines.append(wc)
                    new_lines.append(line)
                parsed["Dockerfile"] = "\n".join(new_lines)

        # Drop any hallucinated non-existent files copied from host in Dockerfile
        if "Dockerfile" in parsed and workspace_path and workspace_path.exists():
            clean_lines: list[str] = []
            for line in parsed["Dockerfile"].splitlines():
                s = line.strip()
                if (s.startswith("COPY ") or s.startswith("ADD ")) and not s.startswith("COPY --from="):
                    rest = s[5:].strip() if s.startswith("COPY ") else s[4:].strip()
                    while rest.startswith("--"):
                        _, _, rest = rest.partition(" ")
                        rest = rest.strip()
                    src_candidates: list[str] = []
                    if rest.startswith("[") and rest.endswith("]"):
                        try:
                            arr = json.loads(rest)
                            if len(arr) >= 2:
                                src_candidates = [str(x) for x in arr[:-1]]
                        except Exception:
                            pass
                    else:
                        tokens = rest.split()
                        if len(tokens) >= 2:
                            src_candidates = tokens[:-1]
                    has_missing_static = False
                    for src in src_candidates:
                        if "*" in src or "?" in src or src.startswith("$"):
                            continue
                        clean_src = src.strip("'\"")
                        target_file = workspace_path / clean_src
                        if not target_file.exists():
                            has_missing_static = True
                            logger.info("Dropping hallucinated COPY source from Dockerfile: %s", clean_src)
                            break
                    if has_missing_static:
                        continue
                clean_lines.append(line)
            parsed["Dockerfile"] = "\n".join(clean_lines)

        # Ensure Dockerfile runner stage copies package manifests or /app when running npm
        if "Dockerfile" in parsed:
            df_content = parsed["Dockerfile"]
            stages = re.split(r"(?i)^\s*FROM\s+", df_content, flags=re.MULTILINE)
            if len(stages) > 1:
                final_stage = stages[-1]
                has_pkg_copy = (
                    bool(re.search(r"(?i)COPY\s+.*package.*\.json", final_stage))
                    or bool(re.search(r"(?i)COPY\s+(\S+\s+)?\.\s+", final_stage))
                    or bool(re.search(r"(?i)COPY\s+.*--from=\S+.*\s+(/app|\.)(\s+|/|$)", final_stage))
                )
                if re.search(r'(?i)(CMD|ENTRYPOINT)\s+.*["\']npm["\']', final_stage) and not has_pkg_copy:
                    fs_lines = final_stage.splitlines()
                    insert_idx = len(fs_lines)
                    for i, line_item in enumerate(fs_lines):
                        if line_item.strip().upper().startswith(("USER ", "CMD ", "ENTRYPOINT ", "EXPOSE ")):
                            insert_idx = i
                            break
                    has_builder = any(re.search(r"(?i)AS\s+builder\b", s) for s in stages[:-1])
                    if has_builder:
                        fs_lines.insert(insert_idx, "COPY --from=builder /app ./")
                    else:
                        fs_lines.insert(insert_idx, "COPY package*.json ./")
                    stages[-1] = "\n".join(fs_lines)
                    parsed["Dockerfile"] = "FROM ".join(stages)

        file_artifacts = [_FileArtifact(path=k, content=v) for k, v in parsed.items()]
        existing_manifests = {}
        if workspace_path and (workspace_path / "package.json").exists():
            with contextlib.suppress(Exception):
                existing_manifests["package.json"] = (workspace_path / "package.json").read_text(encoding="utf-8")
        findings = validate_artifacts(file_artifacts, existing=existing_manifests if existing_manifests else None)
        if findings:
            finding_messages = "; ".join(str(f) for f in findings)
            logger.warning("Ollama AI recovery proposed invalid manifests: %s", finding_messages)
            return RecoveryPlan(
                status="HALTED_MODEL_INVALID_CORRECTION",
                iteration=current_iteration,
                root_cause_category="MODEL_INVALID_CORRECTION",
                operator_message=f"Configured Ollama model ({target_model}) proposed invalid manifests: {finding_messages}",
                suggested_manifests={},
                updated_blueprint=None,
            )

        return RecoveryPlan(
            status="PROPOSED",
            iteration=current_iteration,
            root_cause_category=sync_plan.root_cause_category,
            operator_message=(
                f"ForgeOps AI synthesized repaired deployment manifests via self-hosted model "
                f"({target_model}) to resolve {bundle.gate_identifier} failure."
            ),
            suggested_manifests=parsed,
            updated_blueprint=None,
        )
    except asyncio.CancelledError:
        client_cancelled = False
        if context is not None:
            if isinstance(context, dict):
                client_cancelled = bool(
                    context.get("cancellation_requested")
                    or context.get("cancelled_by_user")
                    or context.get("cancelled_by_operator")
                )
            elif hasattr(context, "get"):
                client_cancelled = bool(
                    context.get("cancellation_requested", False) or context.get("cancelled_by_user", False)
                )
        if not client_cancelled and run is not None:
            client_cancelled = getattr(run, "status", None) in ("cancelling", "cancelled")

        if client_cancelled:
            return RecoveryPlan(
                status="HALTED_CLIENT_CANCELLED",
                iteration=current_iteration,
                root_cause_category="CLIENT_CANCELLED",
                operator_message="AI recovery was cancelled by client / operator.",
                suggested_manifests={},
                updated_blueprint=None,
            )
        return RecoveryPlan(
            status="HALTED_TASK_TIMEOUT",
            iteration=current_iteration,
            root_cause_category="TASK_TIMEOUT",
            operator_message="AI recovery was aborted due to worker task timeout or background shutdown.",
            suggested_manifests={},
            updated_blueprint=None,
        )
    except httpx.TimeoutException as exc:
        logger.warning("Ollama AI recovery timed out: %s", exc)
        return RecoveryPlan(
            status="HALTED_MODEL_TIMEOUT",
            iteration=current_iteration,
            root_cause_category="MODEL_TIMEOUT",
            operator_message=f"Configured Ollama model request to '{target_base_url}' timed out during inference: {exc}",
            suggested_manifests={},
            updated_blueprint=None,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 499:
            return RecoveryPlan(
                status="HALTED_CLIENT_CANCELLED",
                iteration=current_iteration,
                root_cause_category="CLIENT_CANCELLED",
                operator_message="Ollama inference request was closed/cancelled by client (HTTP 499).",
                suggested_manifests={},
                updated_blueprint=None,
            )
        logger.warning("Ollama AI recovery HTTP status error: %s", exc)
        return RecoveryPlan(
            status="HALTED_MODEL_UNAVAILABLE",
            iteration=current_iteration,
            root_cause_category="MODEL_UNAVAILABLE",
            operator_message=f"Configured Ollama model request to '{target_base_url}' failed with HTTP {exc.response.status_code}: {exc}",
            suggested_manifests={},
            updated_blueprint=None,
        )
    except Exception as exc:
        logger.warning("Ollama AI recovery request failed: %s", exc)
        return RecoveryPlan(
            status="HALTED_MODEL_UNAVAILABLE",
            iteration=current_iteration,
            root_cause_category="MODEL_UNAVAILABLE",
            operator_message=f"Configured Ollama model request to '{target_base_url}' failed: {exc}",
            suggested_manifests={},
            updated_blueprint=None,
        )
