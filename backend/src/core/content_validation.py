# SPDX-License-Identifier: FSL-1.1-ALv2
"""Content faults that a path check cannot see, and that a linter is not looking for.

Every check here judges what a file SAYS. The three faults share a property that makes them worth their
own module: each one is present in a file whose existence already scores, so a report built on paths
awards full marks for the exact artifact that carries the defect.

  * A SECRET BAKED INTO AN IMAGE LAYER. An `ENV` that assigns a real credential survives `docker
    history`, survives being overwritten by a later layer, and travels to every registry the image
    reaches. Rotating the value does not help until the image is rebuilt and the old tags are deleted.
  * A PRIVILEGED CONTAINER. `privileged: true` disables essentially every kernel boundary between the
    container and the host, so a compromise stops being contained. It is usually inherited from a
    Stack Overflow answer for a problem a capability or a mount would have solved.
  * A MANIFEST KUBERNETES WILL REJECT. A Deployment with no `metadata.name` is not a weak Deployment, it
    is not a Deployment — `kubectl apply` fails and the deploy stops. Scoring it as present is the most
    misleading thing a readiness report can do, because the score says ready and the cluster says no.

WHY THE OFFENDER IS ALWAYS NAMED WITH A LINE. Every finding carries the path, the line, and the container
or key at fault. "A container is privileged" sends a reader to grep a directory; "container 'sidecar' in
k8s/daemonset.yaml is privileged" at line 34 sends them to the line.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from .manifest_facts import (
    ItemAudit,
    _container_name,
    _containers,
    _line_of,
    _pod_specs,
    _yaml_bodies,
)

#: Environment or build-argument names whose VALUE must never be a literal in an image.
#:
#: Matched on the name because the value cannot be judged: a high-entropy string is as likely to be a
#: cache key as a credential. A name is a declaration of intent — nobody calls a cache key `SECRET_KEY`.
SECRET_NAME_PATTERN: Final = re.compile(
    r"\b([A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL|ACCESS_?KEY)"
    r"[A-Z0-9_]*)\s*=\s*(\S+)",
    re.IGNORECASE,
)

#: Values that are placeholders rather than credentials, and must not be reported.
#:
#: A Dockerfile that declares `ARG NPM_TOKEN=` is doing the RIGHT thing: naming the input without
#: embedding it. Reporting that as a leak trains people to ignore the check, which costs more than the
#: check earns. `${...}` and `$VAR` are substitutions resolved at build time, not literals.
_PLACEHOLDER = re.compile(r"^(\"\"|''|\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|changeme|xxx+|<[^>]*>)$", re.I)


def _is_placeholder(value: str) -> bool:
    stripped = value.strip().strip("\"'")
    return stripped == "" or bool(_PLACEHOLDER.match(value.strip())) or bool(_PLACEHOLDER.match(stripped))


def dockerfile_secrets_audit(body: str, path: str = "Dockerfile") -> ItemAudit:
    """Every `ENV`/`ARG` in the Dockerfile, and whether its value is a literal credential.

    ONLY `ENV` AND `ARG` LINES ARE READ. A secret in a `RUN` line is a different and lesser fault: it
    reaches the build log and the layer's command record, but a `RUN` that pipes a token into a file is
    also the normal shape of a legitimate build. `ENV` is unambiguous — it persists into the running
    container's environment by design.

    `examined == 0` means the Dockerfile declares no environment at all, which cannot hide a secret, so
    the audit passes. That is the opposite of the resource-limits case, where nothing examined means the
    file has not shown its containers are bounded.
    """
    examined = 0
    satisfied = 0
    offender_path = ""
    detail = ""
    line: int | None = None

    for raw_number, raw in enumerate(body.splitlines(), start=1):
        stripped = raw.strip()
        if not re.match(r"^(ENV|ARG)\s", stripped, re.IGNORECASE):
            continue
        for match in SECRET_NAME_PATTERN.finditer(stripped):
            name, value = match.group(1), match.group(2)
            examined += 1
            if _is_placeholder(value):
                satisfied += 1
                continue
            if not offender_path:
                offender_path = path
                line = raw_number
                # The NAME is reported and the value is not. Echoing it into a readiness report would
                # copy the credential into a second place, which is the fault being reported.
                detail = (
                    f"{path} sets {name} to a literal value at line {raw_number}, which is baked into "
                    "the image layer and survives being overwritten later"
                )

    return ItemAudit(
        examined=examined,
        satisfied=satisfied if examined else 0,
        offender_path=offender_path,
        detail=detail,
        line=line,
    )


def dockerfile_has_no_baked_secrets(body: str) -> tuple[bool, str]:
    """True when no `ENV`/`ARG` assigns a literal credential."""
    audit = dockerfile_secrets_audit(body)
    if audit.examined == 0:
        return True, "Dockerfile declares no environment variables that name a credential"
    if audit.satisfied == audit.examined:
        return True, (f"Dockerfile names {audit.examined} credential input(s) without embedding a value")
    return False, audit.detail


#: Pod-level and container-level settings that remove an isolation boundary, and what each one costs.
_ESCALATIONS: Final[tuple[tuple[str, str], ...]] = (
    ("privileged", "runs privileged, which disables the kernel boundaries between it and the host"),
    (
        "allowPrivilegeEscalation",
        "allows privilege escalation, so a setuid binary inside it can gain more than it was given",
    ),
)


def kubernetes_privileged_audit(paths: Iterable[str], contents: Mapping[str, str]) -> ItemAudit:
    """Every container in every manifest, and whether it keeps its isolation boundaries.

    `examined == 0` means no container was found to judge, which is NOT a pass: a manifest that declares
    no container has not shown its containers are unprivileged, and `ItemAudit.passed` requires
    `examined > 0` for exactly this reason.
    """
    examined = 0
    satisfied = 0
    offender_path = ""
    detail = ""
    line: int | None = None

    for path, body in _yaml_bodies(paths, contents):
        for document in _documents_of(body):
            for pod_spec in _pod_specs(document):
                host_network = bool(pod_spec.get("hostNetwork"))
                host_pid = bool(pod_spec.get("hostPID"))
                for index, container in enumerate(_containers(pod_spec)):
                    examined += 1
                    name = _container_name(container, index)
                    security = container.get("securityContext") or {}
                    faults: list[str] = []
                    for key, consequence in _ESCALATIONS:
                        if security.get(key) is True:
                            faults.append(consequence)
                    if host_network:
                        faults.append("shares the host network namespace, so it can reach anything the node can")
                    if host_pid:
                        faults.append("shares the host process namespace and can see every host process")

                    if not faults:
                        satisfied += 1
                        continue
                    if not offender_path:
                        offender_path = path
                        line = _line_of(body, f"name: {name}") or _line_of(body, "privileged")
                        detail = f"container '{name}' in {path} {faults[0]}"

    return ItemAudit(
        examined=examined,
        satisfied=satisfied,
        offender_path=offender_path,
        detail=detail,
        line=line,
    )


def kubernetes_containers_are_unprivileged(paths: Iterable[str], contents: Mapping[str, str]) -> tuple[bool, str]:
    """True when every container keeps the boundaries between it and its host."""
    audit = kubernetes_privileged_audit(paths, contents)
    if audit.examined == 0:
        return False, "no container was found in any Kubernetes manifest to check"
    if audit.satisfied == audit.examined:
        return True, f"all {audit.examined} container(s) run without host privileges"
    return False, audit.detail


#: The fields `kubectl apply` requires of every object, and what their absence costs.
_REQUIRED_FIELDS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("apiVersion",), "has no apiVersion, so the API server cannot tell which schema to validate it"),
    (("kind",), "has no kind, so nothing says what object it is"),
    (("metadata", "name"), "has no metadata.name, so it cannot be created or referred to"),
)


#: Chart-level files that declare `apiVersion` for Helm's own schema rather than Kubernetes'. Excluded
#: from the Kubernetes object claim test by `_claims_to_be_a_kubernetes_object`.
_HELM_CHART_METADATA: Final[frozenset[str]] = frozenset({"chart.yaml", "values.yaml"})


def _claims_to_be_a_kubernetes_object(path: str, document: Mapping[str, Any]) -> bool:
    """Whether this document is asserting that it is a Kubernetes object.

    THIS SCOPING IS THE CHECK'S CORRECTNESS. Without it every YAML file in the repository is judged
    against the Kubernetes API, and the first version reported

        a document in .github/workflows/ci.yml has no apiVersion

    which is true and utterly wrong: a workflow is not a Kubernetes object and was never claiming to be.
    Caught by the fixture that describes a correct repository, which stopped scoring 100.

    A document is judged when it declares `apiVersion` or `kind` — that is the CLAIM, and a claim is what
    makes the missing third field a fault rather than a category error. A Deployment that declares both
    and omits `metadata.name` is asserting it is an object the API server will accept, and it is wrong.
    Paths under `.github/` are excluded outright: whatever they contain, they are not manifests.

    HELM CHART METADATA IS EXCLUDED ON THE SAME GROUND, and missing it was a real defect. A chart's
    `Chart.yaml` declares `apiVersion: v2` — that is the CHART schema version, not a Kubernetes API
    group — and it carries no `kind` because it does not describe an object. Judged by the rule below it
    was examined and failed, reporting

        a document in charts/<name>/chart.yaml has no kind, so nothing says what object it is

    which marked down every repository shipping a Helm chart, including the one this platform generates
    itself: three sound manifests plus one category error scored 18/25 instead of 25/25. The `.github/`
    exclusion above is the same judgement applied to workflows — chart metadata simply was not
    considered when it was written.

    `values.yaml` goes with it: also chart configuration, also not an object. Chart TEMPLATES are
    deliberately NOT excluded by path, because a rendered template is a real Kubernetes object and
    should be judged as one; an unrendered one carries `{{ }}` and does not survive the YAML parser to
    reach this function.
    """
    normalised = path.replace("\\", "/").lower()
    if normalised.startswith(".github/"):
        return False
    if normalised.rsplit("/", 1)[-1] in _HELM_CHART_METADATA:
        return False
    return bool(document.get("apiVersion")) or bool(document.get("kind"))


def kubernetes_schema_audit(paths: Iterable[str], contents: Mapping[str, str]) -> ItemAudit:
    """Every manifest document, and whether it carries the fields the API server requires.

    THE FAULT THIS CATCHES IS TOTAL, NOT PARTIAL. A Deployment with no `metadata.name` is not a weak
    Deployment — `kubectl apply` rejects it and the deploy stops. A report that scores it as present says
    ready while the cluster says no, which is worse than saying nothing.
    """
    examined = 0
    satisfied = 0
    offender_path = ""
    detail = ""
    line: int | None = None

    for path, body in _yaml_bodies(paths, contents):
        for document in _documents_of(body):
            if not _claims_to_be_a_kubernetes_object(path, document):
                continue
            examined += 1
            fault = ""
            for keys, consequence in _REQUIRED_FIELDS:
                value: Any = document
                for key in keys:
                    value = value.get(key) if isinstance(value, Mapping) else None
                if value in (None, ""):
                    fault = consequence
                    break
            if not fault:
                satisfied += 1
                continue
            if not offender_path:
                offender_path = path
                kind = document.get("kind") or "a document"
                line = _line_of(body, "kind:") or 1
                detail = f"{kind} in {path} {fault}"

    return ItemAudit(
        examined=examined,
        satisfied=satisfied,
        offender_path=offender_path,
        detail=detail,
        line=line,
    )


def kubernetes_manifests_are_well_formed(paths: Iterable[str], contents: Mapping[str, str]) -> tuple[bool, str]:
    """True when every manifest document carries apiVersion, kind and metadata.name."""
    audit = kubernetes_schema_audit(paths, contents)
    if audit.examined == 0:
        return False, "no Kubernetes document could be read from the manifests found"
    if audit.satisfied == audit.examined:
        return True, f"all {audit.examined} document(s) declare apiVersion, kind and metadata.name"
    return False, audit.detail


def _documents_of(body: str) -> list[Mapping[str, Any]]:
    """Import-cycle-free access to the private document splitter in `manifest_facts`."""
    from .manifest_facts import _documents

    return _documents(body)
