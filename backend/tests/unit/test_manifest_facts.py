# SPDX-License-Identifier: FSL-1.1-ALv2
"""FR-20's manifest facts, tested against what a substring search would have accepted.

Each case is one way `"limits:" in body` gives the wrong answer. That is the whole reason this module
parses, and a readiness score is a number an operator makes decisions from, so a check that is wrong in
either direction is worse than a check that is absent.
"""

from __future__ import annotations

import pytest
from src.core.manifest_facts import (
    dockerfile_base_pinned,
    dockerfile_healthcheck,
    kubernetes_image_tags_pinned,
    kubernetes_probes,
    kubernetes_resource_limits,
    pipeline_actions_pinned,
    pipeline_runs_tests,
    pipeline_stages_declared,
)

BOUNDED_CONTAINER = """\
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
"""

PROBES = """\
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
          readinessProbe:
            httpGet:
              path: /ready
              port: 8080
"""


def deployment(
    *, resources: str = BOUNDED_CONTAINER, probes: str = PROBES, image: str = "ghcr.io/acme/app:1.2.3"
) -> str:
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  template:
    spec:
      containers:
        - name: app
          image: {image}
{probes}{resources}"""


def evidence(body: str, path: str = "k8s/deployment.yaml") -> tuple[tuple[str, ...], dict[str, str]]:
    return (path,), {path: body}


# ── resource limits: FR-20's named check ─────────────────────────────────────────────────────────


def test_a_fully_bounded_container_passes() -> None:
    paths, contents = evidence(deployment())
    passed, where = kubernetes_resource_limits(paths, contents)
    assert passed is True
    assert where == "k8s/deployment.yaml"


def test_a_container_with_no_resources_block_fails() -> None:
    paths, contents = evidence(deployment(resources=""))
    passed, where = kubernetes_resource_limits(paths, contents)
    assert passed is False
    # The OFFENDING path, which is the single most useful thing a failed check can report.
    assert where == "k8s/deployment.yaml"


def test_a_commented_out_limits_block_does_not_count() -> None:
    """`"limits:" in body` is true for this. It is the case the parsing exists for."""
    commented = "          resources: {}\n          # limits:\n          #   cpu: 500m\n"
    paths, contents = evidence(deployment(resources=commented))
    assert kubernetes_resource_limits(paths, contents)[0] is False


def test_limits_without_requests_fails() -> None:
    """Limits alone leave the scheduler guessing, so it packs a node until the pod cannot start."""
    only_limits = "          resources:\n            limits:\n              cpu: 500m\n              memory: 512Mi\n"
    paths, contents = evidence(deployment(resources=only_limits))
    assert kubernetes_resource_limits(paths, contents)[0] is False


@pytest.mark.parametrize("missing", ["cpu", "memory"])
def test_one_missing_dimension_fails(missing: str) -> None:
    """Memory alone lets a container starve its neighbours of CPU; CPU alone lets it get OOM-killed."""
    partial = BOUNDED_CONTAINER.replace(f"              {missing}: 100m\n", "").replace(
        f"              {missing}: 128Mi\n", ""
    )
    partial = "\n".join(line for line in partial.splitlines(keepends=True) if f"{missing}:" not in line)
    paths, contents = evidence(deployment(resources=partial))
    assert kubernetes_resource_limits(paths, contents)[0] is False


def test_one_unbounded_container_among_several_fails_the_whole_check() -> None:
    """A single unbounded container is enough to evict its neighbours, so `any` would be the wrong test."""
    body = (
        deployment()
        + """\
        - name: sidecar
          image: ghcr.io/acme/sidecar:1.0.0
"""
    )
    paths, contents = evidence(body)
    assert kubernetes_resource_limits(paths, contents)[0] is False


def test_an_unbounded_init_container_fails() -> None:
    """It can exhaust a node just as thoroughly as a main one, and it runs first."""
    body = (
        deployment()
        + """\
      initContainers:
        - name: migrate
          image: ghcr.io/acme/migrate:1.0.0
"""
    )
    paths, contents = evidence(body)
    assert kubernetes_resource_limits(paths, contents)[0] is False


def test_a_configmap_is_not_scored_for_limits() -> None:
    """A ConfigMap has no container, so scoring it would make every repository fail a check it cannot pass."""
    body = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\ndata:\n  limits: none\n"
    paths, contents = evidence(body)
    # No workload at all: reported as not-passed with no evidence, rather than as a failure of a
    # manifest that was never in scope.
    assert kubernetes_resource_limits(paths, contents) == (False, "")


def test_a_cronjob_pod_spec_is_reached() -> None:
    """A CronJob nests its pod spec four levels down; a Deployment nests it two."""
    body = f"""\
apiVersion: batch/v1
kind: CronJob
metadata:
  name: nightly
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: job
              image: ghcr.io/acme/job:1.0.0
{BOUNDED_CONTAINER.replace("          ", "              ")}"""
    paths, contents = evidence(body)
    assert kubernetes_resource_limits(paths, contents)[0] is True


def test_a_bare_pod_is_reached() -> None:
    body = f"""\
apiVersion: v1
kind: Pod
metadata:
  name: app
spec:
  containers:
    - name: app
      image: ghcr.io/acme/app:1.0.0
{BOUNDED_CONTAINER.replace("          ", "      ")}"""
    paths, contents = evidence(body)
    assert kubernetes_resource_limits(paths, contents)[0] is True


def test_an_unparsable_manifest_yields_no_facts() -> None:
    """Falling back to a text search here would give the least trustworthy input the loosest check."""
    paths, contents = evidence("apiVersion: v1\n\tkind: bad tab\n")
    assert kubernetes_resource_limits(paths, contents) == (False, "")


def test_a_non_yaml_path_is_ignored() -> None:
    assert kubernetes_resource_limits(("README.md",), {"README.md": "limits: cpu"}) == (False, "")


def test_every_document_of_a_multi_document_manifest_is_checked() -> None:
    body = deployment() + "---\n" + deployment(resources="")
    paths, contents = evidence(body)
    assert kubernetes_resource_limits(paths, contents)[0] is False


# ── probes ───────────────────────────────────────────────────────────────────────────────────────


def test_both_probes_are_required() -> None:
    paths, contents = evidence(deployment())
    assert kubernetes_probes(paths, contents)[0] is True

    liveness_only = PROBES.split("          readinessProbe:")[0]
    paths, contents = evidence(deployment(probes=liveness_only))
    assert kubernetes_probes(paths, contents)[0] is False


def test_a_job_is_not_scored_for_probes() -> None:
    """A container that is supposed to finish does not need a liveness probe."""
    body = f"""\
apiVersion: batch/v1
kind: Job
metadata:
  name: once
spec:
  template:
    spec:
      containers:
        - name: once
          image: ghcr.io/acme/once:1.0.0
{BOUNDED_CONTAINER}"""
    paths, contents = evidence(body)
    assert kubernetes_probes(paths, contents) == (False, "")


# ── image tags ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "image",
    ["ghcr.io/acme/app:1.2.3", "app@sha256:" + "a" * 64, "registry:5000/acme/app:2.0"],
)
def test_a_pinned_image_passes(image: str) -> None:
    paths, contents = evidence(deployment(image=image))
    assert kubernetes_image_tags_pinned(paths, contents)[0] is True, image


@pytest.mark.parametrize("image", ["acme/app:latest", "acme/app", "registry:5000/acme/app"])
def test_an_unpinned_image_fails(image: str) -> None:
    """An untagged image is `:latest` by default, so the two cases are the same defect."""
    paths, contents = evidence(deployment(image=image))
    assert kubernetes_image_tags_pinned(paths, contents)[0] is False, image


def test_a_registry_port_is_not_mistaken_for_a_tag() -> None:
    """The tag is after the last colon only if that colon comes after the last slash."""
    paths, contents = evidence(deployment(image="registry:5000/acme/app:1.0.0"))
    assert kubernetes_image_tags_pinned(paths, contents)[0] is True


# ── pipeline stages: FR-20's other missing check ──────────────────────────────────────────────────

WORKFLOW_PATH = ".github/workflows/ci.yml"


def workflow(body: str) -> tuple[tuple[str, ...], dict[str, str]]:
    return (WORKFLOW_PATH,), {WORKFLOW_PATH: body}


def test_a_workflow_with_a_runnable_step_has_stages() -> None:
    paths, contents = workflow("name: ci\non:\n  push: {}\njobs:\n  b:\n    steps:\n      - run: make\n")
    assert pipeline_stages_declared(paths, contents)[0] is True


def test_a_workflow_with_jobs_but_no_steps_has_no_stages() -> None:
    """`ci_pipeline_present` matched a PATH, so this file satisfied it while running nothing."""
    paths, contents = workflow("name: ci\non:\n  push: {}\njobs:\n  b:\n    runs-on: ubuntu-latest\n")
    assert pipeline_stages_declared(paths, contents)[0] is False


def test_a_workflow_with_no_trigger_never_runs() -> None:
    paths, contents = workflow("name: ci\njobs:\n  b:\n    steps:\n      - run: make\n")
    assert pipeline_stages_declared(paths, contents)[0] is False


def test_the_bare_on_key_is_found_despite_yaml_reading_it_as_true() -> None:
    """YAML 1.1 parses a bare `on` as the boolean `True`, so `document["on"]` misses it."""
    bare = "name: ci\non:\n  push: {}\njobs:\n  b:\n    steps:\n      - run: make\n"
    quoted = 'name: ci\n"on":\n  push: {}\njobs:\n  b:\n    steps:\n      - run: make\n'
    assert pipeline_stages_declared(*workflow(bare))[0] is True
    assert pipeline_stages_declared(*workflow(quoted))[0] is True


def test_a_reusable_workflow_call_is_a_stage() -> None:
    paths, contents = workflow("on:\n  push: {}\njobs:\n  c:\n    uses: o/r/.github/workflows/x.yml@v1\n")
    assert pipeline_stages_declared(paths, contents)[0] is True


def test_a_file_outside_the_workflow_directory_is_not_a_pipeline() -> None:
    paths = ("k8s/ci.yml",)
    contents = {"k8s/ci.yml": "on:\n  push: {}\njobs:\n  b:\n    steps:\n      - run: make\n"}
    assert pipeline_stages_declared(paths, contents)[0] is False


# ── pipeline tests ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("command", ["pytest -q", "go test ./...", "npm test", "make test", "cargo test"])
def test_a_test_command_is_recognised(command: str) -> None:
    paths, contents = workflow(f"on:\n  push: {{}}\njobs:\n  b:\n    steps:\n      - run: {command}\n")
    assert pipeline_runs_tests(paths, contents)[0] is True, command


def test_a_job_merely_named_test_does_not_count() -> None:
    """A pipeline that does not run the tests reports green for every change."""
    paths, contents = workflow("on:\n  push: {}\njobs:\n  test:\n    steps:\n      - run: echo skipping\n")
    assert pipeline_runs_tests(paths, contents)[0] is False


# ── action pinning ───────────────────────────────────────────────────────────────────────────────

SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"


def test_a_sha_pinned_action_passes() -> None:
    paths, contents = workflow(f"on:\n  push: {{}}\njobs:\n  b:\n    steps:\n      - uses: actions/checkout@{SHA}\n")
    passed, where = pipeline_actions_pinned(paths, contents)
    assert passed is True
    assert where == WORKFLOW_PATH


@pytest.mark.parametrize("reference", ["v4", "main", "11bd719"])
def test_a_mutable_reference_fails(reference: str) -> None:
    """A tag is a promise from the action's owner, not a guarantee to this repository."""
    paths, contents = workflow(
        f"on:\n  push: {{}}\njobs:\n  b:\n    steps:\n      - uses: actions/checkout@{reference}\n"
    )
    assert pipeline_actions_pinned(paths, contents)[0] is False, reference


def test_first_party_actions_are_not_exempt() -> None:
    """Ownership of a namespace is not a substitute for immutability."""
    paths, contents = workflow("on:\n  push: {}\njobs:\n  b:\n    steps:\n      - uses: github/codeql-action@v3\n")
    assert pipeline_actions_pinned(paths, contents)[0] is False


@pytest.mark.parametrize("reference", ["./.github/actions/setup", "docker://alpine:3.20"])
def test_local_and_docker_references_are_exempt(reference: str) -> None:
    paths, contents = workflow(f"on:\n  push: {{}}\njobs:\n  b:\n    steps:\n      - uses: {reference}\n")
    assert pipeline_actions_pinned(paths, contents)[0] is True, reference


def test_a_workflow_with_nothing_to_pin_passes_citing_the_workflow() -> None:
    """Evidence is the workflow itself, not an empty string.

    Returning `""` was the first attempt, on the reasoning that no file had demonstrated pinning.
    `test_every_check_that_passes_names_its_evidence` in `test_readiness_index_evidence.py` rejected it,
    and that invariant is right: a passing check with no evidence tells an operator nothing.
    """
    paths, contents = workflow("on:\n  push: {}\njobs:\n  b:\n    steps:\n      - run: make\n")
    assert pipeline_actions_pinned(paths, contents) == (True, WORKFLOW_PATH)


def test_no_workflow_at_all_passes_with_no_evidence() -> None:
    """Vacuous, and the readiness engine gates this check on a workflow existing, so it is never scored."""
    assert pipeline_actions_pinned((), {}) == (True, "")


# ── Dockerfile ───────────────────────────────────────────────────────────────────────────────────


def test_a_healthcheck_instruction_is_found() -> None:
    assert dockerfile_healthcheck("FROM alpine:3.20\nHEALTHCHECK CMD wget -q -O- localhost:8080/healthz\n") is True


def test_a_commented_healthcheck_does_not_count() -> None:
    assert dockerfile_healthcheck("FROM alpine:3.20\n# HEALTHCHECK todo\n") is False


def test_a_healthcheck_inside_a_value_does_not_count() -> None:
    assert dockerfile_healthcheck('FROM alpine:3.20\nENV NOTE="HEALTHCHECK is missing"\n') is False


@pytest.mark.parametrize(
    "body",
    [
        "FROM alpine:3.20\n",
        "FROM alpine@sha256:" + "b" * 64 + "\n",
        "FROM golang:1.24 AS build\nFROM alpine:3.20\nCOPY --from=build /app /app\n",
        "FROM golang:1.24 AS build\nFROM build\n",
    ],
)
def test_a_pinned_base_passes(body: str) -> None:
    assert dockerfile_base_pinned(body) is True, body


@pytest.mark.parametrize(
    "body",
    [
        "FROM alpine\n",
        "FROM alpine:latest\n",
        "FROM $BASE_IMAGE\n",
        "FROM golang:latest AS build\nFROM alpine:3.20\n",
    ],
)
def test_an_unpinned_base_fails(body: str) -> None:
    """EVERY `FROM`: a multi-stage build whose builder floats is not reproducible."""
    assert dockerfile_base_pinned(body) is False, body


def test_a_dockerfile_with_no_from_is_not_pinned() -> None:
    """Reported as not-pinned rather than trivially pinned: there is nothing to be reproducible about."""
    assert dockerfile_base_pinned("# just a comment\n") is False


def test_a_commented_from_is_not_read() -> None:
    assert dockerfile_base_pinned("# FROM alpine\nFROM alpine:3.20\n") is True
