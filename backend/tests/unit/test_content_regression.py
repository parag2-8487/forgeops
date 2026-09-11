# SPDX-License-Identifier: FSL-1.1-ALv2
"""A generated rewrite may not remove a property the file it replaces already satisfied.

THE REPORTED FAILURE. A user scored a project at 90, followed the recommendation the product printed,
generated, approved, and scored again: 85. Generation had replaced a 1301-byte `k8s/deployment.yaml`
carrying a pinned image, CPU and memory requests and limits, and liveness and readiness probes, with a
345-byte one carrying none of them. Three readiness checks flipped and `orchestration` fell from
275/275 to 195/275. The score was right; the change set was not.

The fixtures below are that file, before and after, trimmed only of comments.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.content_regression import (
    properties_lost,
    properties_to_preserve,
    regression_findings,
)

#: The 1301-byte Deployment that scored 90.
COMPLIANT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: demo-service
spec:
  replicas: 2
  selector:
    matchLabels:
      app.kubernetes.io/name: demo-service
  template:
    metadata:
      labels:
        app.kubernetes.io/name: demo-service
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
      containers:
        - name: demo-service
          image: demo-service:1.0.0
          ports:
            - name: http
              containerPort: 3000
          resources:
            limits:
              cpu: 250m
              memory: 256Mi
            requests:
              cpu: 50m
              memory: 128Mi
          livenessProbe:
            httpGet:
              path: /livez
              port: http
          readinessProbe:
            httpGet:
              path: /readyz
              port: http
"""

#: The 345-byte Deployment the model produced, which the gate now refuses.
DEGRADED = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-3
spec:
  replicas: 1
  selector:
    matchLabels:
      app: test-3
  template:
    metadata:
      labels:
        app: test-3
    spec:
      containers:
        - name: app
          image: test-3:latest
          ports:
            - containerPort: 8000
"""

PATH = "k8s/deployment.yaml"


@dataclass(frozen=True)
class Artifact:
    """The `.path`/`.content` surface the gate reads, without importing the generation domain."""

    path: str
    content: str


class TestTheReportedRegression:
    def test_it_names_every_property_the_rewrite_would_drop(self):
        lost = dict(properties_lost(PATH, COMPLIANT, DEGRADED))

        # Exactly the three readiness checks that flipped from pass to fail on the user's project.
        assert set(lost) == {
            "kubernetes_resource_limits_declared",
            "kubernetes_probes_declared",
            "kubernetes_image_tags_pinned",
        }

    def test_the_finding_says_what_would_be_lost_and_what_to_do(self):
        findings = regression_findings([Artifact(PATH, DEGRADED)], {PATH: COMPLIANT})

        assert findings, "the rewrite must be refused"
        joined = " ".join(findings)
        # The path prefix is what makes D-106's per-file withholding apply without further wiring.
        assert all(f.startswith(f"{PATH}: ") for f in findings)
        # Phrased as a LOSS, because "no liveness probe" reads as a pre-existing gap rather than as a
        # regression this change set would introduce.
        assert "would remove" in joined
        assert "lower the readiness score" in joined
        assert "Modify the existing file instead of replacing it" in joined


class TestWhatItMustNotRefuse:
    def test_identical_content_loses_nothing(self):
        assert properties_lost(PATH, COMPLIANT, COMPLIANT) == ()

    def test_an_unrelated_improvement_is_allowed_through(self):
        better = COMPLIANT.replace("replicas: 2", "replicas: 3")
        assert properties_lost(PATH, COMPLIANT, better) == ()

    def test_a_property_that_was_never_satisfied_is_not_a_regression(self):
        """The narrowest rule that makes the score monotone: it refuses a TRADE, not a gap.

        A Deployment that never declared probes is not made worse by a replacement that also does not,
        and demanding otherwise would turn every small edit into a full remediation.
        """
        assert properties_lost(PATH, DEGRADED, DEGRADED) == ()

    def test_a_rewrite_that_adds_the_missing_properties_is_allowed(self):
        # The direction the product is supposed to move in.
        assert properties_lost(PATH, DEGRADED, COMPLIANT) == ()

    def test_a_create_is_never_judged(self):
        """A file that did not exist cannot have lost anything, and an empty pre-image is not evidence."""
        assert regression_findings([Artifact(PATH, DEGRADED)], {}) == ()
        assert regression_findings([Artifact(PATH, DEGRADED)], {PATH: ""}) == ()
        assert regression_findings([Artifact(PATH, DEGRADED)], {PATH: "   \n"}) == ()

    def test_no_existing_index_disables_the_guard_rather_than_refusing_everything(self):
        assert regression_findings([Artifact(PATH, DEGRADED)], None) == ()

    def test_a_sibling_artifact_is_not_blamed_for_this_one(self):
        """Per-file, so the other artifacts in the run still reach the change set (D-106)."""
        findings = regression_findings(
            [Artifact(PATH, DEGRADED), Artifact("k8s/service.yaml", "apiVersion: v1\nkind: Service\n")],
            {PATH: COMPLIANT},
        )
        assert all(f.startswith(f"{PATH}: ") for f in findings)
        assert not any(f.startswith("k8s/service.yaml") for f in findings)


class TestTheGateActuallyUsesIt:
    """The module being correct is not the same as the gate calling it.

    These go through `GenerationService._validate`, which is what the streaming loop calls, so a future
    refactor that drops the `existing` argument fails here rather than silently restoring the defect.
    """

    @staticmethod
    def _service():
        from src.generation.service import GenerationService

        # No collaborators are touched by `_validate`, so an uninitialised instance is the honest way
        # to exercise the gate without standing up a router, a cache and a breaker set.
        return GenerationService.__new__(GenerationService)

    @staticmethod
    def _file(path: str, content: str):
        from src.generation.service import GeneratedFile

        return GeneratedFile(path=path, content=content)

    def test_the_gate_refuses_a_degrading_rewrite(self):
        passed, findings = self._service()._validate([self._file(PATH, DEGRADED)], {PATH: COMPLIANT})

        assert passed is False
        assert len(findings) == 3, findings

    def test_the_gate_passes_the_same_artifact_as_a_create(self):
        passed, findings = self._service()._validate([self._file(PATH, DEGRADED)], None)

        assert passed is True, findings

    def test_the_gate_passes_a_rewrite_that_keeps_every_property(self):
        kept = COMPLIANT.replace("replicas: 2", "replicas: 4")
        passed, findings = self._service()._validate([self._file(PATH, kept)], {PATH: COMPLIANT})

        assert passed is True, findings

    def test_only_the_offending_artifact_is_withheld(self):
        """D-106's per-file rule applies unchanged, because the findings carry the path prefix."""
        service = self._service()
        sibling = self._file("k8s/service.yaml", "apiVersion: v1\nkind: Service\nmetadata:\n  name: s\n")
        files = [self._file(PATH, DEGRADED), sibling]

        _, findings = service._validate(files, {PATH: COMPLIANT})
        accepted = [f.path for f in files if not any(x.startswith(f"{f.path}: ") for x in findings)]

        assert accepted == ["k8s/service.yaml"]

    def test_stream_generation_accepts_the_existing_contents(self):
        """The route has to be able to supply them, or the gate above never sees a pre-image."""
        import inspect

        from src.generation.service import GenerationService

        assert "existing" in inspect.signature(GenerationService.stream_generation).parameters


class TestUnparseableContent:
    def test_a_rewrite_of_an_unreadable_file_is_not_refused_on_that_basis(self):
        """A validator that cannot read either version establishes nothing.

        Refusing here would block a correct edit whenever a validator met a shape it did not expect,
        which is a worse failure than missing one regression — and the well-formedness check in the
        rest of the gate already covers output that does not parse.
        """
        assert properties_lost(PATH, ": not : valid : yaml :", DEGRADED) == ()

    def test_a_dockerfile_losing_its_healthcheck_is_refused(self):
        before = "FROM python:3.13.1-slim\nUSER 10001\nHEALTHCHECK CMD curl -f http://localhost:8000/health\n"
        after = "FROM python:3.13.1-slim\nUSER 10001\n"
        lost = dict(properties_lost("Dockerfile", before, after))
        assert "dockerfile_healthcheck_present" in lost

    def test_a_dockerfile_losing_its_pinned_base_image_is_refused(self):
        before = "FROM python:3.13.1-slim\nHEALTHCHECK CMD true\n"
        after = "FROM python:latest\nHEALTHCHECK CMD true\n"
        lost = dict(properties_lost("Dockerfile", before, after))
        assert "dockerfile_base_image_pinned" in lost


class TestThePromptIsToldWhatToKeep:
    """The guard alone produces a stalemate; this is what lets the score RISE.

    Without preserve instructions the model keeps emitting the same wholesale rewrite, the gate keeps
    withholding it, and the checks it was meant to fix stay exactly where they were. The score stops
    falling and never climbs. `_preserve_notes` already protected comment blocks and named build stages
    — the visibly structural things — and said nothing about the properties the score measures.
    """

    def test_it_names_every_property_the_file_currently_satisfies(self):
        notes = properties_to_preserve(PATH, COMPLIANT)
        joined = " ".join(notes)

        assert "resources:" in joined
        assert "livenessProbe" in joined and "readinessProbe" in joined
        assert "image tag" in joined and "latest" in joined

    def test_it_never_asks_for_a_property_the_file_does_not_have(self):
        """Otherwise it is an instruction to invent something, and the compiler states facts only."""
        notes = " ".join(properties_to_preserve(PATH, DEGRADED))

        assert "livenessProbe" not in notes
        assert "resources:" not in notes

    def test_an_empty_body_asks_for_nothing(self):
        assert properties_to_preserve(PATH, "") == ()
        assert properties_to_preserve(PATH, "   \n") == ()

    def test_the_compiler_attaches_them_to_a_modify(self):
        """Wiring, not just the helper: a refactor that drops the call fails here."""
        import inspect

        from src.generation import prompt_compiler

        source = inspect.getsource(prompt_compiler)
        assert "properties_to_preserve(path, body)" in source

    def test_a_dockerfile_is_told_to_keep_its_healthcheck_and_pin(self):
        body = "FROM python:3.13.1-slim\nUSER 10001\nHEALTHCHECK CMD curl -f http://localhost:8000/health\n"
        joined = " ".join(properties_to_preserve("Dockerfile", body))

        assert "HEALTHCHECK" in joined
        assert "base image pinned" in joined
