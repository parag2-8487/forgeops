"""Blueprint-grounded prompt compilation for ForgeOps deployment manifest generation.

Derives all Dockerfile, Docker Compose, and Kubernetes generation instructions
strictly from the authoritative ProjectBlueprint contract, with zero hardcoded path assumptions.
"""

from __future__ import annotations

try:
    from src.blueprint.models import ProjectBlueprint, WorkloadType
except ImportError:
    from backend.src.blueprint.models import ProjectBlueprint, WorkloadType


def compile_dockerfile_prompt(blueprint: ProjectBlueprint) -> str:
    """Compiles a strict, blueprint-parameterized prompt for generating a production Dockerfile."""
    bp = blueprint
    source_dir = bp.build_config.source_dir
    lang = bp.runtime.language
    ver = bp.runtime.runtime_version
    pkg_mgr = bp.runtime.package_manager
    framework = bp.runtime.framework or "generic"
    build_cmd = bp.build_config.build_command
    start_cmd = bp.runtime.start_command
    port = bp.network.listen_port
    output_dir = bp.build_config.artifact_output_dir or "dist"

    subfolder_guidance = ""
    if source_dir not in {".", "./", ""}:
        subfolder_guidance = f"""
CRITICAL DIRECTORY STRUCTURE REQUIREMENT:
- The target application lives in subfolder: '{source_dir}'
- Do NOT flatten the repository directory structure arbitrarily.
- Ensure WORKDIR in the builder and runner stages reflects '{source_dir}' or navigates
  to '{source_dir}' before executing build and start commands.
- If copying files, preserve the subdirectory structure so relative imports and framework
  path resolution (e.g. pages, app directories) remain intact.
"""

    workload_guidance = ""
    if bp.workload_type == WorkloadType.STATIC_SPA:
        workload_guidance = f"""
WORKLOAD TYPE: Static Single-Page Application (SPA)
- Stage 1 (Builder): Install dependencies using '{pkg_mgr}', run build '{build_cmd or f"{pkg_mgr} run build"}',
  outputting static files to '{output_dir}'.
- Stage 2 (Runner): Use 'nginx:alpine'. Copy static files from builder '{output_dir}' into '/usr/share/nginx/html'.
- Configure Nginx for SPA fallback (try_files $uri $uri/ /index.html).
- Expose port {port or 80}.
"""
    elif bp.workload_type == WorkloadType.BACKGROUND_WORKER:
        workload_guidance = f"""
WORKLOAD TYPE: Background Worker / Daemon
- This is a headless worker service. Do NOT expose any HTTP port unless explicitly requested.
- Runner stage executes start command: '{start_cmd}'.
"""
    else:
        workload_guidance = f"""
WORKLOAD TYPE: Web Service ({framework})
- Multi-stage build: Stage 1 compiles/builds, Stage 2 packages minimal production runner.
- Expose port {port}.
- Start command: '{start_cmd}'.
"""

    return f"""You are generating an optimized, multi-stage, production-ready Dockerfile for ForgeOps.

PROJECT BLUEPRINT SPECIFICATION:
- Runtime Language: {lang} (Version: {ver})
- Framework: {framework}
- Package Manager: {pkg_mgr}
- Application Source Subdirectory: '{source_dir}'
- Install Command: '{bp.build_config.install_command or f"{pkg_mgr} install"}'
- Build Command: '{build_cmd or "N/A"}'
- Start Command: '{start_cmd}'
- Listen Port: {port or "None"}
{subfolder_guidance}
{workload_guidance}

STRICT PRODUCTION RULES:
1. Multi-Stage Build: Separate dependencies/compilation from the lightweight runtime image.
2. Layer Caching: Copy manifest and lockfiles first, run install, then copy source files.
3. Non-Root User: Run container process as non-root user (e.g. node, appuser) where supported.
4. Clean Output: Output ONLY the raw Dockerfile content. Do not include markdown code fence formatting or commentary.
"""


def compile_compose_prompt(blueprint: ProjectBlueprint, dockerfile_relative_path: str = "Dockerfile") -> str:
    """Compiles a strict prompt for generating a Compose manifest aligned with the blueprint."""
    bp = blueprint
    port = bp.network.listen_port
    svc_name = "app"

    port_mapping = f'- "{port}:{port}"' if port else ""

    return f"""You are generating an authoritative docker-compose.yml file for ForgeOps.

BLUEPRINT SPECIFICATION:
- Service Name: {svc_name}
- Dockerfile Path: {dockerfile_relative_path}
- Build Context: .
- Listen Port: {port or "None"}
- Workload Type: {bp.workload_type.value}

STRICT REQUIREMENTS:
1. Use Compose Specification standard syntax.
2. Define service '{svc_name}' with build context '.' and dockerfile '{dockerfile_relative_path}'.
3. Map ports: {port_mapping}
4. Configure restart policy: 'unless-stopped' (or 'no' if batch job).
5. Output ONLY valid YAML without markdown formatting or commentary.
"""


def compile_kubernetes_prompt(blueprint: ProjectBlueprint, image_name: str = "app:latest") -> str:
    """Compiles a strict prompt for generating Kubernetes Deployment and Service manifests."""
    bp = blueprint
    port = bp.network.listen_port
    app_label = "forgeops-workload"

    service_yaml_req = ""
    if port:
        service_yaml_req = f"""
- Include a Kubernetes Service resource:
  - kind: Service
  - type: ClusterIP (or LoadBalancer)
  - port: {port}
  - targetPort: {port}
"""

    return f"""You are generating production Kubernetes manifests for ForgeOps.

BLUEPRINT SPECIFICATION:
- Image: {image_name}
- Workload Type: {bp.workload_type.value}
- Listen Port: {port or "None"}
- Health Check Path: {bp.network.health_check_path or "/health"}

STRICT REQUIREMENTS:
1. Generate a Deployment resource with label selector 'app: {app_label}'.
2. Set container port to {port or 8080}.
{service_yaml_req}
3. If web service, configure livenessProbe and readinessProbe targeting
   {bp.network.health_check_path or "/"} on port {port or 8080}.
4. Output valid Kubernetes YAML separated by '---' without markdown code blocks.
"""
