# SPDX-License-Identifier: FSL-1.1-ALv2
"""Facts read out of Kubernetes manifests, workflows and Dockerfiles (FR-20).

WHY THIS IS A SEPARATE MODULE AND WHY IT PARSES

FR-20 names five example checks: "Dockerfile exists, multi-stage, non-root user; pipeline stages; K8s
resource limits; `.env.example` exists; no secrets in code". Three of those existed. **Pipeline stages and
K8s resource limits did not** — the readiness engine had twenty checks and neither was among them, so a
manifest with no `resources` block and a workflow with no runnable step both scored full marks for
orchestration and CI.

The checks below could have been regexes over the concatenated file bodies the engine already had, and
that would have been wrong in a way that matters here more than usual, because a readiness score is a
number an operator makes decisions from:

* `"limits:" in body` is true for `# limits: not set yet`, for a `limits:` key under an unrelated CRD, and
  for a `resources: {}` sitting next to a commented-out block. It is false for a manifest that sets limits
  through a `LimitRange` — which is a real answer, just not one a substring can find.
* `"cpu" in body` is true for a container named `cpu-burner`.
* A workflow's `on:` key parses as the BOOLEAN `True` under YAML 1.1, so `document["on"]` misses it
  entirely — the same trap `artifact_checks.py` documents.

So each fact here is read from a parsed document, per file, and a file that does not parse contributes
nothing rather than a guess. Every function returns the PATH that decided the answer, because that is what
`ReadinessCheck.evidence` reports and a failed check whose evidence is empty tells an operator nothing.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping
from typing import Any, Final

import yaml
from pydantic import BaseModel

#: `kind` values whose pod template a resource or probe check applies to. A `ConfigMap` has no container,
#: so scoring it for missing limits would make every repository fail a check it cannot pass.
WORKLOAD_KINDS: Final[frozenset[str]] = frozenset(
    {
        "Pod",
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "ReplicaSet",
        "Job",
        "CronJob",
        "ReplicationController",
    }
)

#: The two resource dimensions that must both be bounded. Memory alone lets a container starve its
#: neighbours of CPU; CPU alone lets it get OOM-killed taking the node with it.
REQUIRED_RESOURCE_KEYS: Final[tuple[str, ...]] = ("cpu", "memory")

#: Probe fields Kubernetes recognises. `startupProbe` alone is not enough — it only gates the other two.
LIVENESS_PROBES: Final[tuple[str, ...]] = ("livenessProbe", "readinessProbe")

#: Step keys that mean the step does something. A step with neither runs nothing.
_RUNNABLE_STEP_KEYS: Final[tuple[str, ...]] = ("run", "uses")

#: Substrings that identify a test-running step. Matched against the COMMAND, not the whole workflow, so a
#: job named "test" that runs nothing does not count.
_TEST_COMMAND_MARKERS: Final[tuple[str, ...]] = (
    "pytest",
    "go test",
    "npm test",
    "npm run test",
    "pnpm test",
    "yarn test",
    "vitest",
    "jest",
    "cargo test",
    "mvn test",
    "gradle test",
    "make test",
    "tox",
    "rspec",
    "phpunit",
    "dotnet test",
    "bun test",
    "playwright test",
)


def _documents(body: str) -> list[Mapping[str, Any]]:
    """Every mapping document in a YAML stream, or none if it does not parse.

    `safe_load_all` rather than `load_all`: these are indexed repository files, so the parser must not be
    able to construct arbitrary Python objects from them.
    """
    try:
        loaded = list(yaml.safe_load_all(body))
    except yaml.YAMLError:
        # A file the runtime itself could not read yields no facts. Falling back to a text search here
        # would mean the least trustworthy input got the loosest check.
        return []
    return [d for d in loaded if isinstance(d, Mapping)]


def _yaml_bodies(paths: Iterable[str], contents: Mapping[str, str]) -> list[tuple[str, str]]:
    """The (path, body) pairs for indexed YAML files, in path order for determinism."""
    out: list[tuple[str, str]] = []
    for path in sorted(paths):
        if not path.lower().endswith((".yaml", ".yml")):
            continue
        body = contents.get(path)
        if body:
            out.append((path, body))
    return out


def _pod_specs(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The pod specs inside one manifest document, whatever wraps them.

    A `Deployment` nests its pod spec two levels down, a `CronJob` four, and a bare `Pod` has it at the
    top. Walking the known shapes rather than searching recursively for a `containers` key, because a
    recursive search would also find the `containers` of an unrelated CRD that happens to use the word.
    """
    kind = str(document.get("kind") or "")
    if kind not in WORKLOAD_KINDS:
        return []
    if kind == "Pod":
        spec = document.get("spec")
        return [spec] if isinstance(spec, Mapping) else []

    candidates: list[Any] = []
    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        return []
    if kind == "CronJob":
        job_template = spec.get("jobTemplate")
        if isinstance(job_template, Mapping):
            job_spec = job_template.get("spec")
            if isinstance(job_spec, Mapping):
                candidates.append(job_spec.get("template"))
    else:
        candidates.append(spec.get("template"))

    out: list[Mapping[str, Any]] = []
    for template in candidates:
        if isinstance(template, Mapping):
            pod_spec = template.get("spec")
            if isinstance(pod_spec, Mapping):
                out.append(pod_spec)
    return out


def _containers(pod_spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every container in a pod spec, including init and ephemeral ones.

    Init containers are INCLUDED deliberately: an unbounded init container can exhaust a node just as
    thoroughly as an unbounded main one, and it runs first.
    """
    out: list[Mapping[str, Any]] = []
    for field in ("containers", "initContainers", "ephemeralContainers"):
        value = pod_spec.get(field)
        if isinstance(value, list):
            out.extend(c for c in value if isinstance(c, Mapping))
    return out


def kubernetes_resource_limits(paths: Iterable[str], contents: Mapping[str, str]) -> tuple[bool, str]:
    """FR-20's named check: does every workload container bound both CPU and memory?

    EVERY container, not any. "One deployment sets limits" is not the property that protects a cluster —
    a single unbounded container is enough to evict its neighbours, so a check that passed on the first
    bounded container would pass exactly the repositories that need the warning.

    Returns `(passed, evidence)`. On failure the evidence is the OFFENDING path, which is the single most
    useful thing a failed check can report; on success it is the path that satisfied it.
    """
    first_bounded = ""
    for path, body in _yaml_bodies(paths, contents):
        for document in _documents(body):
            for pod_spec in _pod_specs(document):
                for container in _containers(pod_spec):
                    resources = container.get("resources")
                    if not isinstance(resources, Mapping):
                        return False, path
                    limits = resources.get("limits")
                    requests = resources.get("requests")
                    if not isinstance(limits, Mapping) or not isinstance(requests, Mapping):
                        # Requests as well as limits. Limits alone leave the scheduler guessing what the
                        # pod needs, so it packs a node until the pod cannot start.
                        return False, path
                    for key in REQUIRED_RESOURCE_KEYS:
                        if key not in limits or key not in requests:
                            return False, path
                    first_bounded = first_bounded or path
    return bool(first_bounded), first_bounded


def kubernetes_probes(paths: Iterable[str], contents: Mapping[str, str]) -> tuple[bool, str]:
    """Does every long-running workload container declare a liveness and a readiness probe?

    `Job` and `CronJob` are EXCLUDED: a container that is supposed to finish does not need a liveness
    probe, and requiring one would score a correct manifest as incomplete.
    """
    first_probed = ""
    for path, body in _yaml_bodies(paths, contents):
        for document in _documents(body):
            if str(document.get("kind") or "") in {"Job", "CronJob"}:
                continue
            for pod_spec in _pod_specs(document):
                for container in _containers(pod_spec):
                    # An init container runs to completion, so the same reasoning applies to it.
                    if container in (pod_spec.get("initContainers") or []):
                        continue
                    if not all(isinstance(container.get(p), Mapping) for p in LIVENESS_PROBES):
                        return False, path
                    first_probed = first_probed or path
    return bool(first_probed), first_probed


def kubernetes_image_tags_pinned(paths: Iterable[str], contents: Mapping[str, str]) -> tuple[bool, str]:
    """Does every container image name a specific version rather than `latest` or nothing at all?

    An untagged image is `:latest` by default, so the two cases are the same defect and are reported
    together. A digest (`@sha256:...`) is the strongest form and passes.
    """
    first_pinned = ""
    for path, body in _yaml_bodies(paths, contents):
        for document in _documents(body):
            for pod_spec in _pod_specs(document):
                for container in _containers(pod_spec):
                    image = container.get("image")
                    if not isinstance(image, str) or not image:
                        return False, path
                    if "@sha256:" in image:
                        first_pinned = first_pinned or path
                        continue
                    # The tag is after the last colon, but only if that colon comes after the last
                    # slash — `registry:5000/app` is a port, not a tag.
                    tail = image.rsplit("/", 1)[-1]
                    _, _, tag = tail.partition(":")
                    if not tag or tag == "latest":
                        return False, path
                    first_pinned = first_pinned or path
    return bool(first_pinned), first_pinned


def _workflow_documents(paths: Iterable[str], contents: Mapping[str, str]) -> list[tuple[str, Mapping[str, Any]]]:
    out: list[tuple[str, Mapping[str, Any]]] = []
    for path, body in _yaml_bodies(paths, contents):
        if ".github/workflows/" not in path.lower().replace("\\", "/"):
            continue
        for document in _documents(body):
            out.append((path, document))
    return out


def _jobs(document: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    jobs = document.get("jobs")
    if not isinstance(jobs, Mapping):
        return []
    return [(str(name), body) for name, body in jobs.items() if isinstance(body, Mapping)]


def _steps(job: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, Mapping)]


def pipeline_stages_declared(paths: Iterable[str], contents: Mapping[str, str]) -> tuple[bool, str]:
    """FR-20's other missing check: does the pipeline actually have stages that run something?

    "A workflow file exists" was the whole of the previous CI check, and a file with a `jobs:` key and no
    steps satisfies it while running nothing. This reads the jobs, requires at least one with a step that
    has a `run` or a `uses`, and requires the workflow to have a trigger at all — a workflow with no `on`
    never executes, so its stages are decoration.

    The `on` key is read at BOTH spellings because YAML 1.1 parses a bare `on` as the boolean `True`.
    """
    for path, document in _workflow_documents(paths, contents):
        trigger = document.get("on", document.get(True))
        if trigger is None:
            continue
        for _, job in _jobs(document):
            if job.get("uses"):
                # A reusable workflow call is a stage: it has no steps of its own by design.
                return True, path
            for step in _steps(job):
                if any(step.get(key) for key in _RUNNABLE_STEP_KEYS):
                    return True, path
    return False, ""


def pipeline_runs_tests(paths: Iterable[str], contents: Mapping[str, str]) -> tuple[bool, str]:
    """Does a pipeline step actually invoke a test runner?

    Matched against the step's COMMAND rather than the workflow text, so a job merely NAMED `test` does
    not count — a pipeline with no tests to run reports green for every change, which is worse than no
    pipeline.
    """
    for path, document in _workflow_documents(paths, contents):
        for _, job in _jobs(document):
            for step in _steps(job):
                command = step.get("run")
                if isinstance(command, str) and any(m in command.lower() for m in _TEST_COMMAND_MARKERS):
                    return True, path
                action = step.get("uses")
                if isinstance(action, str) and "playwright" in action.lower():
                    return True, path
    return False, ""


def pipeline_actions_pinned(paths: Iterable[str], contents: Mapping[str, str]) -> tuple[bool, str]:
    """Is every third-party action pinned to a commit SHA?

    A tag is mutable, so `@v4` is a promise from the action's owner rather than a guarantee to this
    repository — the supply-chain concern GitHub's own hardening guide names.

    `actions/*` and `github/*` are NOT exempt: the whole point of pinning is that ownership of a
    namespace is not a substitute for immutability. Local actions (`./.github/actions/x`) and Docker
    references (`docker://`) are exempt, because neither is fetched by tag.
    """
    first_pinned = ""
    first_workflow = ""
    for path, document in _workflow_documents(paths, contents):
        first_workflow = first_workflow or path
        for _, job in _jobs(document):
            for step in _steps(job):
                action = step.get("uses")
                if not isinstance(action, str) or not action:
                    continue
                if action.startswith("./") or action.startswith("docker://"):
                    continue
                _, _, reference = action.partition("@")
                # A 40-character lowercase hex string is a commit SHA. Anything shorter is a tag or an
                # abbreviated SHA, and an abbreviated SHA is still resolvable to a moving target.
                if len(reference) != 40 or not all(c in "0123456789abcdef" for c in reference):
                    return False, path
                first_pinned = first_pinned or path
    # A workflow that uses no external action has nothing to pin, and passes citing the workflow itself.
    #
    # Returning an EMPTY evidence string here was the first attempt, on the reasoning that no file had
    # demonstrated pinning. `test_every_check_that_passes_names_its_evidence` rejected it, and that
    # invariant is right: a passing check with no evidence tells an operator nothing about why it passed.
    # The workflow is the honest citation — it is the file whose every action reference was examined.
    return True, first_pinned or first_workflow


def dockerfile_healthcheck(body: str) -> bool:
    """Does a Dockerfile declare a HEALTHCHECK?

    Read as an INSTRUCTION at the start of a line, so `# HEALTHCHECK todo` and an `ENV` value containing
    the word do not count — the same distinction `artifact_checks.validate_dockerfile` makes for `USER`.
    """
    for raw in body.splitlines():
        line = raw.strip()
        if line.upper().startswith("HEALTHCHECK "):
            return True
    return False


def dockerfile_base_pinned(body: str) -> bool:
    """Is every `FROM` pinned to a digest or an explicit tag that is not `latest`?

    EVERY `FROM`, because a multi-stage build whose builder floats is a build that is not reproducible
    even when its final stage is pinned.

    A stage alias (`FROM builder`) is exempt: it refers to an earlier stage in the same file, which is as
    pinned as that stage is.
    """
    stages: set[str] = set()
    saw_from = False
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts or parts[0].upper() != "FROM" or len(parts) < 2:
            continue
        saw_from = True
        image = parts[1]
        # `FROM x AS y` registers y as a stage alias for later `FROM` instructions.
        if len(parts) >= 4 and parts[2].upper() == "AS":
            stages.add(parts[3].lower())
        if image.lower() in stages:
            continue
        if image.startswith("$"):
            # `FROM $BASE_IMAGE` defers the decision to a build argument, which cannot be read here.
            # Treated as unpinned rather than assumed pinned, because assuming would credit the
            # repository for a property this file does not establish.
            return False
        if "@sha256:" in image:
            continue
        tail = image.rsplit("/", 1)[-1]
        _, _, tag = tail.partition(":")
        if not tag or tag == "latest":
            return False
    return saw_from


def basename(path: str) -> str:
    return posixpath.basename(path)


# ─────────────────────────────────────────────────────────────────────────────
# Counting audits
#
# The `(passed, evidence)` functions above short-circuit on the FIRST offender, which is the right
# shape for a yes/no answer and the wrong shape for two things a reader needs:
#
#   * a PROPORTION. Points were all-or-nothing, so a manifest with four correct containers and one
#     without limits scored exactly what five wrong ones scored. A user fixing four of five saw the
#     number not move, which teaches that the score does not respond to work.
#   * a NAMED PROPERTY. "the offending path" says which file; it does not say which container, which
#     of requests or limits, or which of cpu or memory. That is the difference between a report a
#     reader can act on and one they have to reverse-engineer.
#
# These return both, and never guess: `examined == 0` means there was nothing to judge, which is a
# different answer from "everything passed" and is reported as such.
# ─────────────────────────────────────────────────────────────────────────────


class ItemAudit(BaseModel):
    """How many things a check examined, how many satisfied it, and what the first failure was.

    `detail` is written for a reader who does not already know the answer: it names the file, the item
    within it, and the specific property that was absent.
    """

    #: Total items considered. Zero means there was nothing of this kind to judge.
    examined: int = 0
    #: How many of them satisfied the property.
    satisfied: int = 0
    #: The path holding the first item that did not.
    offender_path: str = ""
    #: A sentence naming the file, the item and the missing property.
    detail: str = ""
    #: The 1-based line the offending item begins on, when it could be located in the body. `None`
    #: when the body was not available or the item could not be tied to a line — an invented line
    #: number is worse than none, because it sends a reader to the wrong place confidently.
    line: int | None = None

    model_config = {"frozen": True}

    @property
    def passed(self) -> bool:
        """True only when something was examined and all of it satisfied the property.

        `examined == 0` is False, deliberately: a repository with no containers has not demonstrated
        that its containers are bounded, and the vacuous truth would score it as though it had.
        """
        return self.examined > 0 and self.satisfied == self.examined


def _line_of(body: str, needle: str) -> int | None:
    """The 1-based line where `needle` first appears, or None.

    Used to point a reader at a container by its `name:` entry. Returns None rather than 0 or 1 when
    the text is not found, because a caller must be able to tell "line 1" from "unknown".
    """
    if not needle:
        return None
    for number, text in enumerate(body.splitlines(), start=1):
        if needle in text:
            return number
    return None


def _container_name(container: Mapping[str, Any], index: int) -> str:
    name = container.get("name")
    return str(name) if isinstance(name, str) and name else f"container[{index}]"


def kubernetes_resource_limits_audit(paths: Iterable[str], contents: Mapping[str, str]) -> ItemAudit:
    """Count containers that bound both cpu and memory, in both requests and limits."""
    examined = 0
    satisfied = 0
    offender_path = ""
    detail = ""
    line: int | None = None

    for path, body in _yaml_bodies(paths, contents):
        for document in _documents(body):
            for pod_spec in _pod_specs(document):
                for index, container in enumerate(_containers(pod_spec)):
                    examined += 1
                    name = _container_name(container, index)
                    resources = container.get("resources")
                    missing: list[str] = []
                    if not isinstance(resources, Mapping):
                        missing.append("no resources block at all")
                    else:
                        for section in ("requests", "limits"):
                            block = resources.get(section)
                            if not isinstance(block, Mapping):
                                missing.append(f"no {section}")
                                continue
                            for key in REQUIRED_RESOURCE_KEYS:
                                if key not in block:
                                    missing.append(f"no {section}.{key}")
                    if missing:
                        if not offender_path:
                            offender_path = path
                            detail = f"container {name!r} in {path} declares " + ", ".join(missing)
                            line = _line_of(body, f"name: {name}")
                    else:
                        satisfied += 1

    return ItemAudit(
        examined=examined,
        satisfied=satisfied,
        offender_path=offender_path,
        detail=detail,
        line=line,
    )


def kubernetes_probes_audit(paths: Iterable[str], contents: Mapping[str, str]) -> ItemAudit:
    """Count long-running containers declaring both a liveness and a readiness probe.

    `Job` and `CronJob` are excluded for the reason `kubernetes_probes` gives: a container meant to
    finish does not need a liveness probe, and demanding one would mark a correct manifest wrong.
    """
    examined = 0
    satisfied = 0
    offender_path = ""
    detail = ""
    line: int | None = None

    for path, body in _yaml_bodies(paths, contents):
        for document in _documents(body):
            if str(document.get("kind") or "") in {"Job", "CronJob"}:
                continue
            for pod_spec in _pod_specs(document):
                init = pod_spec.get("initContainers") or []
                for index, container in enumerate(_containers(pod_spec)):
                    if container in init:
                        continue
                    examined += 1
                    name = _container_name(container, index)
                    missing = [p for p in LIVENESS_PROBES if not isinstance(container.get(p), Mapping)]
                    if missing:
                        if not offender_path:
                            offender_path = path
                            detail = f"container {name!r} in {path} declares no " + " and no ".join(missing)
                            line = _line_of(body, f"name: {name}")
                    else:
                        satisfied += 1

    return ItemAudit(
        examined=examined,
        satisfied=satisfied,
        offender_path=offender_path,
        detail=detail,
        line=line,
    )


def kubernetes_image_tags_audit(paths: Iterable[str], contents: Mapping[str, str]) -> ItemAudit:
    """Count container images that name an immutable reference rather than a floating one."""
    examined = 0
    satisfied = 0
    offender_path = ""
    detail = ""
    line: int | None = None

    for path, body in _yaml_bodies(paths, contents):
        for document in _documents(body):
            for pod_spec in _pod_specs(document):
                for index, container in enumerate(_containers(pod_spec)):
                    image = container.get("image")
                    if not isinstance(image, str) or not image:
                        continue
                    examined += 1
                    name = _container_name(container, index)
                    reference = image.rsplit("/", 1)[-1]
                    if "@sha256:" in image:
                        satisfied += 1
                        continue
                    if ":" not in reference:
                        problem = f"image {image!r} carries no tag, so it resolves to latest"
                    elif reference.rsplit(":", 1)[-1] == "latest":
                        problem = f"image {image!r} uses the latest tag"
                    else:
                        satisfied += 1
                        continue
                    if not offender_path:
                        offender_path = path
                        detail = f"container {name!r} in {path}: {problem}"
                        line = _line_of(body, f"image: {image}")

    return ItemAudit(
        examined=examined,
        satisfied=satisfied,
        offender_path=offender_path,
        detail=detail,
        line=line,
    )


def pipeline_actions_pinned_audit(paths: Iterable[str], contents: Mapping[str, str]) -> ItemAudit:
    """Count `uses:` references pinned to a full commit SHA.

    Names the ACTION and the line, which is what the recommendation needed: "a tag is mutable" states
    a principle, and a reader still has to find which action in which file on which line.
    """
    examined = 0
    satisfied = 0
    offender_path = ""
    detail = ""
    line: int | None = None

    for path, document in _workflow_documents(paths, contents):
        body = contents.get(path.lower(), "") or contents.get(path, "")
        for _job_name, job in _jobs(document):
            for step in _steps(job):
                uses = step.get("uses")
                if not isinstance(uses, str) or not uses:
                    continue
                examined += 1
                reference = uses.rsplit("@", 1)[-1] if "@" in uses else ""
                if len(reference) == 40 and all(c in "0123456789abcdef" for c in reference.lower()):
                    satisfied += 1
                    continue
                if not offender_path:
                    offender_path = path
                    shown = reference or "no reference at all"
                    detail = (
                        f"{path} uses {uses.rsplit('@', 1)[0]!r} at {shown!r}, which is a moving "
                        "pointer rather than a commit"
                    )
                    line = _line_of(body, uses)

    return ItemAudit(
        examined=examined,
        satisfied=satisfied,
        offender_path=offender_path,
        detail=detail,
        line=line,
    )
