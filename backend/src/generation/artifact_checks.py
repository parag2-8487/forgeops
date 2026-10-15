# SPDX-License-Identifier: FSL-1.1-ALv2
"""§11.5.5's deterministic gate, over documents that are actually parsed.

WHAT THIS REPLACES
------------------
`ArtifactGenerationService._validate` decided whether a user may see a generated artifact by testing
for substrings::

    for required in ("apiVersion:", "kind:", "metadata:", "spec:"):
        if required not in manifest:
            findings.append(...)

That accepts a manifest whose `apiVersion` is a comment, whose `kind` appears inside a string, whose
indentation is broken so it is one scalar rather than a mapping, or which is not YAML at all — every
one of those contains all four substrings. It also accepts a `metadata` with no `name`, which no
cluster will take. The gate could only fail an artifact that failed to mention the right words.

Everything here parses the document and then checks its shape. That is the difference between "the
file mentions apiVersion" and "the file declares a Kubernetes object".

WHY THE BACKEND VALIDATES AT ALL, GIVEN FR-27
---------------------------------------------
FR-27 is "the **local agent** validates artifacts before the user sees them", and it is satisfied by
the agent's six `validate.*` operations, which shell out to `docker compose`, `kubectl`, `helm`, `tofu`,
`yamllint` and `trivy` on the user's own machine. None of those binaries is in the backend image and
none should be: a server that runs `docker compose config` over text a model produced is a server
running a tool over untrusted input, and the tools belong where the workspace is.

So there are two gates, deliberately, and they answer different questions:

* **This one** runs in the generation loop and answers "is this document well formed and the right
  shape?" — cheap, offline, deterministic, and the thing a repair iteration can act on.
* **The agent's** runs before an apply and answers "will the real tools accept it?" — which needs a
  cluster, a Docker daemon and a provider cache.

An artifact that fails here never reaches a user, which is what makes this a gate rather than advice.
An artifact that passes here has been parsed and shaped, not grepped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

import yaml

#: Kubernetes objects must declare these. `metadata.name` is included because a manifest with an
#: anonymous object is rejected by every cluster, and the substring gate accepted it.
_K8S_REQUIRED: Final[tuple[str, ...]] = ("apiVersion", "kind")

#: A Dockerfile instruction, at the start of a line, case-insensitive as Docker treats them.
_INSTRUCTION: Final[re.Pattern[str]] = re.compile(r"^\s*([A-Za-z]+)\s", re.MULTILINE)


def _load_documents(content: str) -> tuple[list[Any] | None, str | None]:
    """Parse a possibly multi-document YAML file.

    Returns `(documents, None)` or `(None, reason)`. A parse failure is a finding rather than an
    exception, because the loop's whole purpose is to hand a reason back to the model.
    """
    try:
        documents = [doc for doc in yaml.safe_load_all(content) if doc is not None]
    except yaml.YAMLError as exc:
        # `yaml.YAMLError` renders with line and column, which is exactly what a repair attempt needs.
        return None, f"is not parsable YAML: {str(exc).splitlines()[0]}"
    return documents, None


def validate_dockerfile(content: str) -> list[str]:
    """Check a Dockerfile's instructions, rather than searching its text.

    `FROM` is required first because Docker requires it, and `USER` because a container that runs as
    root is a deterministic defect the readiness rubric also reports. Both were previously checked with
    `startswith` and `in`, so `# FROM alpine` satisfied the first and the word `USER` inside a comment
    or an environment value satisfied the second.
    """
    findings: list[str] = []
    if not content.strip():
        return ["Dockerfile is empty"]

    instructions: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        found = _INSTRUCTION.match(stripped)
        if found:
            instructions.append(found.group(1).upper())

    if not instructions:
        return ["Dockerfile contains no instructions, only comments or blank lines"]
    # `ARG` before `FROM` is legal and common, so the first instruction that is not an ARG must be FROM.
    first = next((name for name in instructions if name != "ARG"), None)
    if first != "FROM":
        findings.append(
            f"Dockerfile's first instruction is {first or 'absent'}, not FROM (ARG may precede FROM; nothing else may)"
        )
    if "USER" not in instructions:
        findings.append("Dockerfile does not drop root with a USER instruction")
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("COPY ") and not stripped.startswith("COPY --from="):
            after_copy = stripped[5:].strip()
            if not after_copy.startswith("[") and (" " in after_copy):
                tokens = after_copy.split()
                non_flags = [t for t in tokens if not t.startswith("--")]
                if len(non_flags) > 2:
                    findings.append(
                        f"Dockerfile instruction '{stripped}' has unquoted arguments containing spaces. "
                        "Docker splits on spaces. Use Docker JSON array syntax for paths with spaces, e.g.: "
                        'COPY ["dir with space/package*.json", "./dir with space/"]'
                    )
        if re.search(r"COPY\s+--from=\S+\s+\./(dist|build|public|out)\b", stripped, re.IGNORECASE):
            findings.append(
                "Dockerfile uses relative path in COPY --from=... (e.g. './dist'). "
                "Use absolute path from builder WORKDIR (e.g. '/app/dist') to avoid '/dist: not found' build failures."
            )
        if re.search(r"(?i)RUN\s+.*(npm run build|next build|go build|cargo build|gradle build|mvn package|pip install|npm install|npm ci|yarn install|pnpm install)\s+.*\|\|\s*(true|:|exit\s+0)\b", stripped):
            findings.append(
                "Dockerfile suppresses build or install failure with '|| true' or '|| exit 0'. "
                "Build errors must return a non-zero exit code to prevent deploying broken artifacts."
            )
        if "--frozen-lockfile" in stripped and "npm" in stripped:
            findings.append("Dockerfile uses invalid flag '--frozen-lockfile' with npm. Use 'npm ci' or 'npm install'.")
        if "frontent" in stripped.lower():
            findings.append(
                "Dockerfile references hallucinated/typoed directory 'frontent'. Copy files directly from root: "
                "`COPY package*.json ./`."
            )
    # Check if final stage invokes npm start or node without package.json copied
    stages = re.split(r"(?i)^\s*FROM\s+", content, flags=re.MULTILINE)
    if len(stages) > 1:
        final_stage = stages[-1]
        has_pkg_copy = (
            bool(re.search(r"(?i)COPY\s+.*package.*\.json", final_stage))
            or bool(re.search(r"(?i)COPY\s+(\S+\s+)?\.\s+", final_stage))
            or bool(re.search(r"(?i)COPY\s+.*--from=\S+.*\s+(/app|\.)(\s+|/|$)", final_stage))
        )
        if re.search(r'(?i)(CMD|ENTRYPOINT)\s+.*["\']npm["\']', final_stage) and not has_pkg_copy:
            findings.append(
                "Dockerfile final stage executes 'npm' but does not copy 'package*.json' "
                "(e.g. `COPY package*.json ./`). This will fail with ENOENT: Could not read package.json at runtime."
            )
    # Check workspace package installation ordering
    lines = content.splitlines()
    has_workspace_build = any(
        re.search(r"--workspace[=\s]|workspaces\b", line, re.IGNORECASE) for line in lines
    )
    has_ws_copy = any(
        (re.search(r"COPY\s+.*package.*\.json\s+.*(/\w+|\./\w+)", line, re.IGNORECASE)
         or re.search(r"COPY\s+\[\s*\"[^\"]+package[^\"]*\.json\"\s*,\s*\"[^\"]+\"\s*\]", line, re.IGNORECASE))
        and not line.strip().startswith("COPY --from=")
        for line in lines
    )
    if has_workspace_build or has_ws_copy:
        install_line_idx = -1
        first_ws_copy_idx = -1
        for idx, line in enumerate(lines):
            s = line.strip()
            if s.startswith("RUN ") and any(cmd in s for cmd in ["npm ci", "npm install", "yarn install", "pnpm install"]):
                if install_line_idx == -1:
                    install_line_idx = idx
            if s.startswith("COPY ") and not s.startswith("COPY --from="):
                parts = s.split()
                is_ws_manifest_copy = False
                if len(parts) >= 3 and not parts[1].startswith("["):
                    src = parts[1]
                    if ("/" in src or "\\" in src) and "package" in src.lower():
                        is_ws_manifest_copy = True
                elif "[" in s and "package" in s.lower():
                    m = re.search(r'\[\s*"([^"]+)"', s)
                    if m and ("/" in m.group(1) or "\\" in m.group(1)):
                        is_ws_manifest_copy = True
                elif re.search(r"COPY\s+.*package.*\.json\s+.*(/\w+|\./\w+)", s, re.IGNORECASE):
                    is_ws_manifest_copy = True

                if is_ws_manifest_copy and first_ws_copy_idx == -1:
                    first_ws_copy_idx = idx
        if install_line_idx != -1 and (first_ws_copy_idx == -1 or install_line_idx < first_ws_copy_idx):
            findings.append(
                "Dockerfile runs dependency installation (`RUN npm ci` or `install`) before copying workspace package manifests. "
                "Workspace manifests (e.g. `COPY <workspace>/package*.json ./<workspace>/`) must be copied BEFORE running install "
                "so monorepo workspace dependencies can be resolved."
            )
    findings.extend(_port_disagreements(content))
    return findings


#: A listen port in a start command: an explicit flag, or the port of a `host:port` listen address.
#:
#: A BARE NUMBER IS NOT ACCEPTED. A CMD is full of numbers that are not ports — heap sizes, timeouts,
#: worker counts — and treating the first one as a port is how a container ends up published on a
#: number it never binds, which is the defect this whole check exists to catch.
_CMD_PORT_PATTERNS: Final = (
    re.compile(r"--(?:port|listen-port|http-port)[= ]+(\d{1,5})\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)-p[= ]+(\d{1,5})\b", re.IGNORECASE),
    re.compile(r"(?:tcp|http)://[^:\"'\s]*:(\d{1,5})\b", re.IGNORECASE),
    re.compile(r"(?:\d{1,3}\.){3}\d{1,3}:(\d{1,5})\b"),
)

_EXPOSE = re.compile(r"^\s*EXPOSE\s+(.+?)\s*$", re.IGNORECASE)
_ENV_PORT = re.compile(r"(?:^|\s)(?:PORT|LISTEN_PORT|APP_PORT|HTTP_PORT|SERVER_PORT)=[\"']?(\d{1,5})", re.IGNORECASE)


def _port_disagreements(content: str) -> list[str]:
    """Reject a Dockerfile that states two different ports for the same service.

    THE DEFECT THIS PREVENTS, and it reached a user. A generated Dockerfile declared `EXPOSE 8080`
    while its start command bound 3000 and its `ENV PORT` said 3000. Nothing compared the two, so the
    file shipped. The deployment then published the port `EXPOSE` named, the process listened on the
    port the CMD named, and a browser got `ERR_EMPTY_RESPONSE` from a container that was genuinely
    healthy — the healthcheck runs inside and probed the right port, so it passed, and every status
    the platform reported was true. A port mismatch is invisible to every check that looks at one
    line at a time, which is why this one looks at the file as a whole.

    `EXPOSE` is the line reported as wrong, deliberately. The start command is what DETERMINES the
    port: it is the process's own argument. `EXPOSE` is a declaration no runtime reads, so when they
    disagree it is the declaration that is mistaken, and naming it tells the model which line to
    change rather than leaving it to pick.

    A Dockerfile with no `EXPOSE`, or whose CMD states no port, produces no finding. Both are legal
    and neither is a contradiction — this checks for DISAGREEMENT, not for completeness.
    """
    exposed: list[int] = []
    cmd_ports: set[int] = set()
    env_ports: set[int] = set()

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        found = _EXPOSE.match(stripped)
        if found:
            for token in found.group(1).split():
                # `EXPOSE 8080/tcp` is legal; the protocol is not part of the number.
                number = token.split("/", 1)[0]
                if number.isdigit() and 1 <= int(number) <= 65535:
                    exposed.append(int(number))
            continue

        upper = stripped.upper()
        if upper.startswith("CMD ") or upper.startswith("ENTRYPOINT "):
            for pattern in _CMD_PORT_PATTERNS:
                match = pattern.search(stripped)
                if match and 1 <= int(match.group(1)) <= 65535:
                    cmd_ports.add(int(match.group(1)))
                    break
        elif upper.startswith("ENV "):
            for match in _ENV_PORT.finditer(stripped):
                if 1 <= int(match.group(1)) <= 65535:
                    env_ports.add(int(match.group(1)))

    if not exposed:
        return []

    # The start command is the authority. `ENV PORT` is consulted only when the command states no port
    # of its own, because a program may read the variable or ignore it, whereas a flag it was launched
    # with is not in doubt.
    bound = cmd_ports or env_ports
    if not bound:
        return []

    if set(exposed) & bound:
        return []

    bound_list = ", ".join(str(port) for port in sorted(bound))
    source = "its start command" if cmd_ports else "its ENV PORT"
    return [
        f"Dockerfile declares EXPOSE {exposed[0]} but {source} binds {bound_list}. "
        f"A published port that nothing listens on accepts the connection and closes it, which reaches "
        f"a browser as an empty response from a container that reports healthy. Change EXPOSE to "
        f"{sorted(bound)[0]}, or bind the port EXPOSE names."
    ]


def validate_kubernetes(content: str) -> list[str]:
    """Check that a manifest declares Kubernetes objects."""
    documents, reason = _load_documents(content)
    if reason is not None:
        return [f"Kubernetes manifest {reason}"]
    if not documents:
        return ["Kubernetes manifest declares no object"]

    findings: list[str] = []
    for index, document in enumerate(documents, start=1):
        prefix = "Kubernetes manifest" if len(documents) == 1 else f"Kubernetes manifest document {index}"
        if not isinstance(document, Mapping):
            findings.append(f"{prefix} is a {type(document).__name__}, not a mapping")
            continue
        for key in _K8S_REQUIRED:
            value = document.get(key)
            if not isinstance(value, str) or not value.strip():
                findings.append(f"{prefix} has no usable {key}")
        metadata = document.get("metadata")
        if not isinstance(metadata, Mapping):
            findings.append(f"{prefix} has no metadata mapping")
        else:
            name = metadata.get("name")
            if not isinstance(name, str) or not name.strip():
                findings.append(f"{prefix} has no metadata.name, so no cluster will accept it")
    return findings


def validate_github_workflow(content: str) -> list[str]:
    """Check that a workflow would run: real triggers, real jobs, real steps."""
    documents, reason = _load_documents(content)
    if reason is not None:
        return [f"GitHub Actions workflow {reason}"]
    if not documents:
        return ["GitHub Actions workflow is empty"]
    document = documents[0]
    if not isinstance(document, Mapping):
        return ["GitHub Actions workflow is not a mapping"]

    findings: list[str] = []
    # `on` is YAML 1.1's boolean true, so a workflow's trigger key parses as `True` rather than `"on"`.
    # Reading only `document["on"]` therefore finds nothing on a perfectly valid workflow — this is the
    # same trap that made yamllint's `truthy` rule reject every workflow until its config was fixed.
    trigger = document.get("on", document.get(True))
    if trigger is None or trigger == {} or trigger == []:
        findings.append("GitHub Actions workflow declares no `on` trigger, so it can never run")

    jobs = document.get("jobs")
    if not isinstance(jobs, Mapping) or not jobs:
        findings.append("GitHub Actions workflow declares no jobs")
        return findings

    for name, job in jobs.items():
        if not isinstance(job, Mapping):
            findings.append(f"job `{name}` is not a mapping")
            continue
        if "uses" in job:
            # A reusable-workflow call needs no runner or steps.
            continue
        if not job.get("runs-on"):
            findings.append(f"job `{name}` has no `runs-on`, so no runner will pick it up")
        steps = job.get("steps")
        if not isinstance(steps, Sequence) or isinstance(steps, str) or not steps:
            findings.append(f"job `{name}` has no steps")
            continue
        for position, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping):
                findings.append(f"job `{name}` step {position} is not a mapping")
            elif not (step.get("uses") or step.get("run")):
                findings.append(f"job `{name}` step {position} has neither `uses` nor `run`")
    return findings


def validate_helm_chart_metadata(content: str) -> list[str]:
    """Check a `Chart.yaml`. Helm REQUIRES SemVer 2 rather than preferring it."""
    documents, reason = _load_documents(content)
    if reason is not None:
        return [f"Chart.yaml {reason}"]
    if not documents or not isinstance(documents[0], Mapping):
        return ["Chart.yaml declares no chart"]
    chart = documents[0]

    findings: list[str] = []
    if chart.get("apiVersion") not in ("v1", "v2"):
        findings.append("Chart.yaml apiVersion must be v1 or v2")
    if not str(chart.get("name", "")).strip():
        findings.append("Chart.yaml has no name")
    version = str(chart.get("version", "")).strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version):
        findings.append(f"Chart.yaml version {version or '(absent)'!r} is not SemVer 2, which Helm requires")
    return findings


def validate_opentofu(content: str) -> list[str]:
    """Check an OpenTofu configuration declares at least one top-level block.

    HCL is not YAML and the backend has no HCL parser, so this is a structural check rather than a
    parse — and it says so. `tofu validate` in the agent is what actually compiles it, which is the
    honest division: this catches an empty or prose-only file, that catches a type error.
    """
    text = content.strip()
    if not text:
        return ["OpenTofu configuration is empty"]
    blocks = re.findall(
        r"^\s*(terraform|provider|resource|variable|output|module|data|locals)\b",
        text,
        re.MULTILINE,
    )
    if not blocks:
        return [
            "OpenTofu configuration declares no top-level block "
            "(terraform, provider, resource, variable, output, module, data or locals)"
        ]
    if text.count("{") != text.count("}"):
        return ["OpenTofu configuration has unbalanced braces"]
    return []


def validate_compose(content: str) -> list[str]:
    """Check a Compose file declares services with images or builds."""
    documents, reason = _load_documents(content)
    if reason is not None:
        return [f"Compose file {reason}"]
    if not documents or not isinstance(documents[0], Mapping):
        return ["Compose file declares nothing"]
    services = documents[0].get("services")
    if not isinstance(services, Mapping) or not services:
        return ["Compose file declares no services"]

    findings: list[str] = []
    for name, service in services.items():
        if not isinstance(service, Mapping):
            findings.append(f"compose service `{name}` is not a mapping")
            continue
        if not (service.get("image") or service.get("build")):
            findings.append(f"compose service `{name}` has neither an image nor a build")
        ports = service.get("ports")
        if ports is not None and (not isinstance(ports, Sequence) or isinstance(ports, str)):
            findings.append(f"compose service `{name}` has a `ports` that is not a list")
    return findings


#: Which checker applies to which artifact, chosen by path. A path this does not recognise is checked
#: as YAML when it looks like YAML and left alone otherwise — inventing a checker for an unknown file
#: would block artifacts for failing rules nobody wrote.
_BY_EXACT_PATH: Final[dict[str, Any]] = {
    "Dockerfile": validate_dockerfile,
    "docker-compose.yml": validate_compose,
    "docker-compose.yaml": validate_compose,
    "Chart.yaml": validate_helm_chart_metadata,
}


def checker_for(path: str) -> Any | None:
    """Return the checker for one artifact path, or None when nothing applies."""
    normalised = path.replace("\\", "/")
    if normalised in _BY_EXACT_PATH:
        return _BY_EXACT_PATH[normalised]
    tail = normalised.rsplit("/", 1)[-1]
    if tail in _BY_EXACT_PATH:
        return _BY_EXACT_PATH[tail]
    if "/.github/workflows/" in f"/{normalised}":
        return validate_github_workflow
    if normalised.startswith("k8s/") or "/k8s/" in normalised:
        return validate_kubernetes
    if normalised.endswith(".tf"):
        return validate_opentofu
    return None


def _extract_dockerfile_container_ports(content: str) -> set[int]:
    """Extract all exposed or listening container ports from a Dockerfile."""
    exposed: set[int] = set()
    cmd_ports: set[int] = set()
    env_ports: set[int] = set()

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        found = _EXPOSE.match(stripped)
        if found:
            for token in found.group(1).split():
                number = token.split("/", 1)[0]
                if number.isdigit() and 1 <= int(number) <= 65535:
                    exposed.add(int(number))
            continue

        upper = stripped.upper()
        if upper.startswith("CMD ") or upper.startswith("ENTRYPOINT "):
            for pattern in _CMD_PORT_PATTERNS:
                match = pattern.search(stripped)
                if match and 1 <= int(match.group(1)) <= 65535:
                    cmd_ports.add(int(match.group(1)))
                    break
        elif upper.startswith("ENV "):
            for match in _ENV_PORT.finditer(stripped):
                if 1 <= int(match.group(1)) <= 65535:
                    env_ports.add(int(match.group(1)))

    return cmd_ports or env_ports or exposed


_INFRASTRUCTURE_SERVICE_NAMES: Final[set[str]] = {
    "postgres",
    "postgresql",
    "db",
    "database",
    "redis",
    "cache",
    "memcached",
    "mysql",
    "mariadb",
    "mongo",
    "mongodb",
    "rabbitmq",
    "broker",
    "kafka",
    "zookeeper",
    "minio",
    "localstack",
    "jaeger",
    "prometheus",
    "grafana",
    "mailhog",
    "elasticsearch",
}


def _is_infrastructure_service(svc_name: str, svc_data: Mapping[str, Any]) -> bool:
    """Checks whether a service is an auxiliary/infrastructure container."""
    name_clean = svc_name.lower().replace("-", "").replace("_", "")
    if any(
        infra in name_clean
        for infra in ("postgres", "postgresql", "redis", "mysql", "mariadb", "mongo", "mongodb")
    ):
        return True
    if svc_name.lower() in _INFRASTRUCTURE_SERVICE_NAMES:
        return True
    image = str(svc_data.get("image") or "").lower()
    if any(
        image.startswith(prefix)
        for prefix in (
            "postgres:",
            "redis:",
            "mysql:",
            "mongo:",
            "mariadb:",
            "rabbitmq:",
            "memcached:",
        )
    ):
        return True
    return False


def _extract_compose_service_specs(content: str) -> dict[str, dict[str, Any]]:
    """Extracts application service specifications (context, image, container ports) from Compose."""
    documents, reason = _load_documents(content)
    if not documents or reason is not None or not isinstance(documents[0], Mapping):
        return {}
    services = documents[0].get("services")
    if not isinstance(services, Mapping):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for svc_name, svc in services.items():
        if not isinstance(svc, Mapping):
            continue
        if _is_infrastructure_service(str(svc_name), svc):
            continue

        ports: set[int] = set()
        svc_ports = svc.get("ports")
        if isinstance(svc_ports, Sequence) and not isinstance(svc_ports, str):
            for p in svc_ports:
                if isinstance(p, int) and 1 <= p <= 65535:
                    ports.add(p)
                elif isinstance(p, str):
                    token = p.strip().strip("'\"").split("/", 1)[0]
                    parts = token.split(":")
                    target = parts[-1]  # Always container target port
                    if target.isdigit() and 1 <= int(target) <= 65535:
                        ports.add(int(target))
        expose = svc.get("expose")
        if isinstance(expose, Sequence) and not isinstance(expose, str):
            for e in expose:
                s = str(e).strip().split("/", 1)[0]
                if s.isdigit() and 1 <= int(s) <= 65535:
                    ports.add(int(s))

        build_info = svc.get("build")
        build_ctx = ""
        dockerfile_name = "Dockerfile"
        if isinstance(build_info, str):
            build_ctx = build_info
        elif isinstance(build_info, Mapping):
            build_ctx = str(build_info.get("context") or "")
            dockerfile_name = str(build_info.get("dockerfile") or "Dockerfile")

        result[str(svc_name)] = {
            "ports": ports,
            "build_context": build_ctx,
            "dockerfile": dockerfile_name,
        }
    return result


def _extract_compose_container_ports(content: str) -> set[int]:
    """Extract container target ports mapped or exposed for application services in Docker Compose."""
    specs = _extract_compose_service_specs(content)
    all_ports: set[int] = set()
    for svc in specs.values():
        all_ports.update(svc["ports"])
    return all_ports


def _extract_k8s_workload_ports(content: str) -> set[int]:
    """Extract containerPort entries declared on Kubernetes pods/workloads."""
    ports: set[int] = set()
    documents, reason = _load_documents(content)
    if not documents or reason is not None:
        return ports
    for doc in documents:
        if not isinstance(doc, Mapping):
            continue
        kind = str(doc.get("kind", ""))
        spec = doc.get("spec")
        if not isinstance(spec, Mapping):
            continue
        pod_spec = spec
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
            template = spec.get("template")
            if isinstance(template, Mapping):
                pod_spec = template.get("spec")
        if not isinstance(pod_spec, Mapping):
            continue
        containers = pod_spec.get("containers")
        if isinstance(containers, Sequence) and not isinstance(containers, str):
            for c in containers:
                if not isinstance(c, Mapping):
                    continue
                c_ports = c.get("ports")
                if isinstance(c_ports, Sequence) and not isinstance(c_ports, str):
                    for cp in c_ports:
                        if isinstance(cp, Mapping):
                            c_port = cp.get("containerPort")
                            if isinstance(c_port, int) and 1 <= c_port <= 65535:
                                ports.add(c_port)
                            elif isinstance(c_port, str) and c_port.isdigit() and 1 <= int(c_port) <= 65535:
                                ports.add(int(c_port))
    return ports


def _extract_k8s_service_ports(content: str) -> set[int]:
    """Extract targetPort (or port) entries declared on Kubernetes Services."""
    ports: set[int] = set()
    documents, reason = _load_documents(content)
    if not documents or reason is not None:
        return ports
    for doc in documents:
        if not isinstance(doc, Mapping):
            continue
        if str(doc.get("kind", "")) != "Service":
            continue
        spec = doc.get("spec")
        if not isinstance(spec, Mapping):
            continue
        svc_ports = spec.get("ports")
        if isinstance(svc_ports, Sequence) and not isinstance(svc_ports, str):
            for sp in svc_ports:
                if isinstance(sp, Mapping):
                    tp = sp.get("targetPort")
                    if isinstance(tp, int) and 1 <= tp <= 65535:
                        ports.add(tp)
                    elif isinstance(tp, str) and tp.isdigit() and 1 <= int(tp) <= 65535:
                        ports.add(int(tp))
                    elif tp is None:
                        p = sp.get("port")
                        if isinstance(p, int) and 1 <= p <= 65535:
                            ports.add(p)
                        elif isinstance(p, str) and p.isdigit() and 1 <= int(p) <= 65535:
                            ports.add(int(p))
    return ports


def _check_k8s_service_and_workload_consistency(files: Sequence[Any]) -> list[str]:
    """Verify Service selectors match workload labels and Ingress references exist."""
    findings: list[str] = []
    deployments: list[tuple[str, dict[str, str], str]] = []
    services: list[tuple[str, dict[str, str], str]] = []
    ingresses: list[tuple[str, list[str]]] = []

    for artifact in files:
        path = getattr(artifact, "path", "")
        content = getattr(artifact, "content", "")
        norm = path.replace("\\", "/")
        if not (norm.startswith("k8s/") or "/k8s/" in norm or norm.endswith((".yaml", ".yml"))):
            continue
        docs, reason = _load_documents(content)
        if not docs or reason is not None:
            continue
        for doc in docs:
            if not isinstance(doc, Mapping):
                continue
            kind = str(doc.get("kind", ""))
            metadata = doc.get("metadata") or {}
            name = str(metadata.get("name", "")) if isinstance(metadata, Mapping) else ""
            spec = doc.get("spec") or {}
            if not isinstance(spec, Mapping):
                continue
            if kind in ("Deployment", "StatefulSet", "DaemonSet"):
                template = spec.get("template") or {}
                t_meta = template.get("metadata") or {} if isinstance(template, Mapping) else {}
                labels = t_meta.get("labels") or {} if isinstance(t_meta, Mapping) else {}
                if isinstance(labels, Mapping):
                    deployments.append((path, {str(k): str(v) for k, v in labels.items()}, name))
            elif kind == "Service":
                selector = spec.get("selector") or {}
                if isinstance(selector, Mapping):
                    services.append((path, {str(k): str(v) for k, v in selector.items()}, name))
            elif kind == "Ingress":
                backend_names: list[str] = []
                rules = spec.get("rules") or []
                if isinstance(rules, Sequence) and not isinstance(rules, str):
                    for r in rules:
                        if isinstance(r, Mapping):
                            http = r.get("http") or {}
                            if isinstance(http, Mapping):
                                paths = http.get("paths") or []
                                if isinstance(paths, Sequence) and not isinstance(paths, str):
                                    for p in paths:
                                        if isinstance(p, Mapping):
                                            backend = p.get("backend") or {}
                                            if isinstance(backend, Mapping):
                                                svc = backend.get("service") or {}
                                                if isinstance(svc, Mapping) and svc.get("name"):
                                                    backend_names.append(str(svc["name"]))
                ingresses.append((path, backend_names))

    for s_path, selector, _s_name in services:
        if not selector:
            continue
        for d_path, labels, _d_name in deployments:
            if not labels:
                continue
            mismatch = False
            for k, v in selector.items():
                if labels.get(k) != v:
                    mismatch = True
                    break
            if mismatch:
                findings.append(
                    f"cross-artifact: {s_path} selector {selector} does not match {d_path} pod labels {labels}. "
                    "Service will route traffic to zero pods."
                )

    service_names = {s_name for _, _, s_name in services if s_name}
    if service_names:
        for i_path, backend_names in ingresses:
            for b_name in backend_names:
                if b_name not in service_names:
                    findings.append(
                        f"cross-artifact: {i_path} references backend service '{b_name}' "
                        f"which does not match any Service metadata.name ({sorted(service_names)})."
                    )

    return findings


def validate_cross_artifact_consistency(files: Sequence[Any]) -> list[str]:
    """Verify that ports and service names agree across Dockerfile, Compose, and Kubernetes.

    Multi-service repositories are validated per-service: distinct microservices legitimately listen on
    different ports. Infrastructure services (postgres, redis) are excluded from application port checks.
    """
    findings: list[str] = []

    dockerfiles: dict[str, set[int]] = {}
    compose_specs: dict[str, dict[str, dict[str, Any]]] = {}
    k8s_workloads: dict[str, set[int]] = {}
    k8s_services: dict[str, set[int]] = {}

    for artifact in files:
        path = getattr(artifact, "path", "")
        content = getattr(artifact, "content", "")
        norm = path.replace("\\", "/")

        if norm == "Dockerfile" or norm.endswith("/Dockerfile"):
            df_ports = _extract_dockerfile_container_ports(content)
            if df_ports:
                dockerfiles[path] = df_ports
        elif norm in ("docker-compose.yml", "docker-compose.yaml") or norm.endswith(
            ("/docker-compose.yml", "/docker-compose.yaml")
        ):
            specs = _extract_compose_service_specs(content)
            if specs:
                compose_specs[path] = specs
        elif norm.startswith("k8s/") or "/k8s/" in norm or norm.endswith((".yaml", ".yml")):
            kp_ports = _extract_k8s_workload_ports(content)
            if kp_ports:
                k8s_workloads[path] = kp_ports
            sp_ports = _extract_k8s_service_ports(content)
            if sp_ports:
                k8s_services[path] = sp_ports

    # 1. Reconcile Dockerfile(s) against Docker Compose application services
    for cp_path, services in compose_specs.items():
        all_app_ports: set[int] = set()
        for svc_data in services.values():
            all_app_ports.update(svc_data["ports"])

        for df_path, df_ports in dockerfiles.items():
            df_dir = (
                df_path.rsplit("/Dockerfile", 1)[0].replace("\\", "/").lower()
                if "/Dockerfile" in df_path
                else "."
            )

            matched_svc_ports: set[int] | None = None
            if len(services) == 1:
                # Single service application
                matched_svc_ports = next(iter(services.values()))["ports"]
            else:
                for svc_name, svc_data in services.items():
                    ctx = str(svc_data.get("build_context") or "").lower().rstrip("/")
                    s_name_norm = svc_name.lower().replace("-", "").replace("_", "")
                    dir_norm = df_dir.replace("-", "").replace("_", "").lower()
                    if ctx == df_dir or (
                        df_dir != "."
                        and (ctx.endswith(df_dir) or dir_norm in s_name_norm or s_name_norm in dir_norm)
                    ):
                        matched_svc_ports = svc_data["ports"]
                        break

            if matched_svc_ports is not None:
                if matched_svc_ports and not (df_ports & matched_svc_ports):
                    findings.append(
                        f"cross-artifact: port mismatch between {df_path} {sorted(df_ports)} and {cp_path} container port {sorted(matched_svc_ports)}. "
                        "Matching service must target the same container port."
                    )
            elif all_app_ports and not (df_ports & all_app_ports):
                findings.append(
                    f"cross-artifact: port mismatch between {df_path} {sorted(df_ports)} and {cp_path} application container ports {sorted(all_app_ports)}. "
                    "All application artifacts must target a matching container port."
                )

    # 2. Reconcile Kubernetes workloads and services
    for w_path, w_ports in k8s_workloads.items():
        for s_path, s_ports in k8s_services.items():
            if not (w_ports & s_ports):
                findings.append(
                    f"cross-artifact: port mismatch between {w_path} containerPort {sorted(w_ports)} and {s_path} targetPort {sorted(s_ports)}. "
                    "Service targetPort must match container listening port."
                )

    # 3. Reconcile root Dockerfile against single Kubernetes workload if single-service
    if len(dockerfiles) == 1 and len(k8s_workloads) == 1:
        df_path, df_ports = next(iter(dockerfiles.items()))
        w_path, w_ports = next(iter(k8s_workloads.items()))
        if not (df_ports & w_ports):
            findings.append(
                f"cross-artifact: port mismatch between {df_path} {sorted(df_ports)} and {w_path} {sorted(w_ports)}. "
                "Workload containerPort must match Dockerfile EXPOSE port."
            )

    findings.extend(_check_k8s_service_and_workload_consistency(files))
    return findings


def validate_artifacts(files: Sequence[Any], existing: Mapping[str, str] | None = None) -> list[str]:
    """Check every generated artifact that has a checker, and report every finding.

    EVERY finding rather than the first: a repair iteration is handed this list, and telling a model
    about one problem at a time turns a single repair into three.
    """
    findings: list[str] = []
    for artifact in files:
        checker = checker_for(artifact.path)
        if checker is None:
            continue
        for finding in checker(artifact.content):
            findings.append(f"{artifact.path}: {finding}")
    findings.extend(validate_cross_artifact_consistency(files))

    if existing:
        ws_dirs: list[str] = []
        if "package.json" in existing:
            try:
                pkg_data = json.loads(existing["package.json"])
                ws_entry = pkg_data.get("workspaces")
                if isinstance(ws_entry, list):
                    ws_dirs.extend([w for w in ws_entry if isinstance(w, str) and "*" not in w])
                elif isinstance(ws_entry, dict) and "packages" in ws_entry:
                    ws_dirs.extend([w for w in ws_entry["packages"] if isinstance(w, str) and "*" not in w])
            except Exception:
                pass

        if ws_dirs:
            for artifact in files:
                norm_p = getattr(artifact, "path", "").replace("\\", "/")
                if norm_p == "Dockerfile" or norm_p.endswith("/Dockerfile"):
                    df_lines = str(getattr(artifact, "content", "")).splitlines()
                    install_idx = -1
                    copied_workspaces: set[str] = set()
                    copied_all = False
                    for idx, line in enumerate(df_lines):
                        s = line.strip()
                        if s.startswith("RUN ") and any(cmd in s for cmd in ["npm ci", "npm install", "yarn install", "pnpm install"]):
                            if install_idx == -1:
                                install_idx = idx
                        if s.startswith("COPY ") and not s.startswith("COPY --from="):
                            if s.startswith("COPY . .") or s.startswith("COPY . /"):
                                if install_idx == -1:
                                    copied_all = True
                            for w in ws_dirs:
                                if (w in s or f'"{w}' in s) and "package" in s.lower() and (install_idx == -1 or idx < install_idx):
                                    copied_workspaces.add(w)

                    if install_idx != -1 and not copied_all and len(copied_workspaces) < len(ws_dirs):
                        missing_ws = [w for w in ws_dirs if w not in copied_workspaces]
                        findings.append(
                            f"{artifact.path}: Workspace repository runs dependency installation before copying workspace package manifests. "
                            f"Workspace manifests ({', '.join(missing_ws)}) must be copied before RUN install to prevent missing executables (exit code 127)."
                        )

    return findings
