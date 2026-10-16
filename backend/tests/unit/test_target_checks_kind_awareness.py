# SPDX-License-Identifier: FSL-1.1-ALv2
"""A container check is not a check about every manifest.

THE DEFECT THIS PINS. `unsatisfied_targets` maps a produced artifact to the checks its KIND targets, and
the kind comes from the PATH — so `k8s/configmap.yaml` was held answerable for
`kubernetes_containers_unprivileged` and `kubernetes_resource_limits_declared`. A ConfigMap has no
containers, so it could never satisfy them, so D-106's per-file rule withheld a perfectly correct
artifact for failing to be something it is not. The same shape as blaming `build.yml` for `ci.yml`'s
unpinned action, which the function's own comment already records, one level further down.

Found by a test named for the ConfigMap failing with a finding about a Dockerfile, then about the
ConfigMap itself — the failure message naming the wrong artifact twice is what made it worth chasing.

THE DIRECTION OF THE FIX MATTERS. It excuses only a check the document cannot possibly satisfy, decided
by the document's own `kind:`. A manifest whose kind cannot be read is still judged, so an unparsable or
mid-edit document is not excused; and a Deployment missing its probes is refused exactly as before.
"""

from __future__ import annotations

import pytest
from src.core.target_checks import unsatisfied_targets

pytestmark = pytest.mark.mandatory

CONFIGMAP = """apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
data:
  k: v
"""

SERVICE = """apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  ports:
    - port: 80
"""

#: A Deployment with none of the container properties. It MUST still be refused.
BARE_DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  template:
    spec:
      containers:
        - name: web
          image: web:latest
"""

UNREADABLE = """apiVersion: apps/v1
metadata:
  name: no-kind-at-all
"""


def _findings_for(path: str, body: str) -> tuple[str, ...]:
    return unsatisfied_targets({path: body})


class TestAContainerFreeDocumentIsNotJudgedByContainerChecks:
    @pytest.mark.parametrize(
        ("path", "body"),
        [("k8s/configmap.yaml", CONFIGMAP), ("k8s/service.yaml", SERVICE)],
    )
    def test_no_container_check_is_attributed_to_it(self, path: str, body: str) -> None:
        findings = _findings_for(path, body)

        for check in (
            "kubernetes_containers_unprivileged",
            "kubernetes_resource_limits_declared",
            "kubernetes_probes_declared",
            "kubernetes_image_tags_pinned",
        ):
            assert not any(check in finding for finding in findings), findings


class TestTheGateIsStillStrict:
    def test_a_deployment_without_the_properties_is_still_refused(self) -> None:
        findings = _findings_for("k8s/deployment.yaml", BARE_DEPLOYMENT)

        # Three of the four, because a Deployment is exactly what these checks are about.
        assert any("kubernetes_resource_limits_declared" in f for f in findings), findings
        assert any("kubernetes_probes_declared" in f for f in findings), findings
        assert any("kubernetes_image_tags_pinned" in f for f in findings), findings
        # `kubernetes_containers_unprivileged` is NOT asserted here, and the reason is worth writing
        # down rather than quietly omitting: this fixture declares no `securityContext` at all, and the
        # scorer treats an absent one as not-privileged rather than as a failure. Asserting it would be
        # asserting a behaviour the product does not have. What this test is for is that the kind-aware
        # exclusion did not stop a Deployment being judged, and three findings establish that.
        assert findings

    def test_a_manifest_whose_kind_cannot_be_read_is_still_judged(self) -> None:
        """`None` means "judge it": an unparsable manifest must not be excused by the same door."""
        findings = _findings_for("k8s/mystery.yaml", UNREADABLE)

        assert any("kubernetes_" in finding for finding in findings), findings

    def test_a_configmap_beside_a_bare_deployment_does_not_excuse_the_deployment(self) -> None:
        """The exclusion is per document, not per run: one container-free file must not cover for a peer."""
        findings = unsatisfied_targets({"k8s/configmap.yaml": CONFIGMAP, "k8s/deployment.yaml": BARE_DEPLOYMENT})

        assert any("kubernetes_probes_declared" in f for f in findings), findings
