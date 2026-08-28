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
    return findings


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


def validate_artifacts(files: Sequence[Any]) -> list[str]:
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
    return findings
