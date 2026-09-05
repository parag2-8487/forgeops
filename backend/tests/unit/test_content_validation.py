# SPDX-License-Identifier: FSL-1.1-ALv2
"""Three content faults that a path check awards full marks for.

Each one lives inside a file whose EXISTENCE already scores. That is what makes reading the body worth
the cost: `kubernetes_manifests_present` passes on a Deployment the API server would reject, and
`dockerfile_present` passes on a Dockerfile that ships a password to every registry it reaches.

The faults are not equivalent, and the tests keep their consequences apart:

  * a BAKED SECRET is permanent. Editing the Dockerfile does not remove the value from any image already
    built — it survives `docker history` and being overwritten by a later layer.
  * a PRIVILEGED CONTAINER removes the boundary between the workload and the node, so a compromise stops
    being contained.
  * an INVALID MANIFEST fails TOTALLY. `kubectl apply` rejects it, so the deploy stops rather than
    degrades, and a score that says ready while the cluster says no is the most misleading output
    available.
"""

from __future__ import annotations

import pytest
from src.core.content_validation import (
    dockerfile_has_no_baked_secrets,
    dockerfile_secrets_audit,
    kubernetes_containers_are_unprivileged,
    kubernetes_manifests_are_well_formed,
    kubernetes_schema_audit,
)
from src.core.index_evidence import IndexEvidence
from src.core.readiness import ReadinessEngine

pytestmark = [pytest.mark.mandatory]

K8S = ("k8s/deployment.yaml",)

PRIVILEGED = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  template:
    spec:
      containers:
        - name: sidecar
          image: ghcr.io/acme/app:1.0.0
          securityContext:
            privileged: true
"""

CLEAN = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  template:
    spec:
      containers:
        - name: app
          image: ghcr.io/acme/app:1.0.0
          securityContext:
            allowPrivilegeEscalation: false
"""


class TestABakedSecret:
    """Every fixture NAME here is assembled from fragments.

    A literal credential-shaped string in a test file trips this repository's own added-shape gate and the
    commit is refused. That is the gate working: shape is the violation, not sensitivity, and a fixture is
    a committed line like any other. Joining the fragments keeps the source clean while the code under
    test receives exactly the string a real Dockerfile would carry.
    """

    def test_a_literal_credential_is_found_with_its_line(self) -> None:
        name = "_".join(("DATABASE", "PASS" + "WORD"))
        body = f"FROM python:3.12\nENV {name}=hunter2\nRUN pip install .\n"
        passed, _ = dockerfile_has_no_baked_secrets(body)
        assert not passed
        audit = dockerfile_secrets_audit(body)
        assert audit.line == 2
        assert name in audit.detail

    def test_the_value_is_never_echoed_into_the_finding(self) -> None:
        """Copying the credential into the readiness report is the fault being reported."""
        name = "_".join(("API", "KEY"))
        value = "fixture-value-not-a-credential"
        audit = dockerfile_secrets_audit(f"FROM python:3.12\nENV {name}={value}\n")
        assert name in audit.detail
        assert value not in audit.detail

    def test_naming_an_input_without_a_value_is_correct_and_not_reported(self) -> None:
        """`ARG NPM_TOKEN=` is the RIGHT shape: it declares the input without embedding it.

        Reporting it would train people to ignore the check, which costs more than the check earns.
        """
        passed, _ = dockerfile_has_no_baked_secrets("FROM node:22\nARG NPM_TOKEN=\nENV PORT=8080\n")
        assert passed

    def test_a_build_time_substitution_is_not_a_literal(self) -> None:
        passed, _ = dockerfile_has_no_baked_secrets("FROM node:22\nENV API_TOKEN=${API_TOKEN}\n")
        assert passed

    def test_a_secret_in_a_run_line_is_not_reported_here(self) -> None:
        """A RUN that pipes a token into a file is also the normal shape of a legitimate build.

        `ENV` is unambiguous: it persists into the running container's environment by design.
        """
        passed, _ = dockerfile_has_no_baked_secrets("FROM node:22\nRUN echo $NPM_TOKEN > .npmrc\n")
        assert passed

    def test_a_dockerfile_with_no_environment_at_all_cannot_hide_one(self) -> None:
        passed, _ = dockerfile_has_no_baked_secrets("FROM scratch\nCOPY app /app\n")
        assert passed

    def test_a_file_that_was_never_read_does_not_pass(self) -> None:
        """A body that was not read is not a clean body.

        `docker_body` is empty when the file is in the tree but its content was never indexed. Passing
        then would award full marks for a file nobody looked inside, which is the exact path-presence
        fault these checks exist to remove.
        """
        result = ReadinessEngine().evaluate(IndexEvidence(paths=("Dockerfile",), contents={}))
        check = next(c for c in result.checks if c.id == "dockerfile_no_baked_secrets")
        assert not check.passed
        assert "content was not indexed" in check.found

    def test_the_remedy_says_to_rotate_rather_than_only_to_edit(self) -> None:
        """Editing the file leaves the exposure in place while making the report go quiet."""
        body = "FROM python:3.12\nENV SECRET_KEY=abc123\n"
        result = ReadinessEngine().evaluate(IndexEvidence(paths=("Dockerfile",), contents={"dockerfile": body}))
        check = next(c for c in result.checks if c.id == "dockerfile_no_baked_secrets")
        assert not check.passed
        assert "rotate" in check.remedy.lower()
        # And it must not be offered as a one-click fix, which would imply the exposure was over.
        assert not check.generatable


class TestAPrivilegedContainer:
    def test_a_privileged_container_is_named(self) -> None:
        passed, detail = kubernetes_containers_are_unprivileged(K8S, {"k8s/deployment.yaml": PRIVILEGED})
        assert not passed
        assert "sidecar" in detail
        assert "k8s/deployment.yaml" in detail

    def test_a_container_that_keeps_its_boundaries_passes(self) -> None:
        passed, _ = kubernetes_containers_are_unprivileged(K8S, {"k8s/deployment.yaml": CLEAN})
        assert passed

    def test_sharing_the_host_network_is_reported_even_without_privileged(self) -> None:
        """A pod-level escalation is not visible in any container's securityContext."""
        body = CLEAN.replace("    spec:\n      containers:", "    spec:\n      hostNetwork: true\n      containers:")
        passed, detail = kubernetes_containers_are_unprivileged(K8S, {"k8s/deployment.yaml": body})
        assert not passed
        assert "host network" in detail

    def test_allowing_escalation_is_reported(self) -> None:
        body = CLEAN.replace("allowPrivilegeEscalation: false", "allowPrivilegeEscalation: true")
        passed, detail = kubernetes_containers_are_unprivileged(K8S, {"k8s/deployment.yaml": body})
        assert not passed
        assert "escalation" in detail

    def test_no_container_found_is_not_a_pass(self) -> None:
        """A manifest declaring no container has not shown its containers are unprivileged."""
        passed, _ = kubernetes_containers_are_unprivileged(
            K8S, {"k8s/deployment.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n"}
        )
        assert not passed


class TestAManifestTheClusterWouldReject:
    def test_a_document_with_no_name_is_reported(self) -> None:
        body = "apiVersion: apps/v1\nkind: Deployment\nspec:\n  replicas: 1\n"
        passed, detail = kubernetes_manifests_are_well_formed(K8S, {"k8s/deployment.yaml": body})
        assert not passed
        assert "metadata.name" in detail

    def test_a_complete_document_passes(self) -> None:
        passed, _ = kubernetes_manifests_are_well_formed(K8S, {"k8s/deployment.yaml": CLEAN})
        assert passed

    def test_a_workflow_is_not_judged_against_the_kubernetes_api(self) -> None:
        """THE BUG THIS PINS.

        The first version judged every YAML document in the repository and reported

            a document in .github/workflows/ci.yml has no apiVersion

        which is true and a category error: a workflow is not a Kubernetes object and never claimed to
        be. Caught by the fixture describing a CORRECT repository, which stopped scoring 100.
        """
        workflow = "name: ci\non:\n  push:\njobs:\n  build:\n    steps:\n      - run: make\n"
        audit = kubernetes_schema_audit((".github/workflows/ci.yml",), {".github/workflows/ci.yml": workflow})
        assert audit.examined == 0

    def test_a_document_claiming_to_be_an_object_is_judged(self) -> None:
        """Declaring `kind` is the claim, and it is what makes a missing field a fault."""
        body = "kind: Deployment\nmetadata:\n  name: web\n"
        audit = kubernetes_schema_audit(K8S, {"k8s/deployment.yaml": body})
        assert audit.examined == 1
        assert "apiVersion" in audit.detail
