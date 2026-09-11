# SPDX-License-Identifier: FSL-1.1-ALv2
"""Invariant 2: a change set that would lower the readiness score is refused.

WHY THIS IS SEPARATE FROM THE PER-FILE GUARD

`content_regression` compares an artifact against the one it replaces, property by property. It
catches the case that prompted it: a rewrite that drops the probes from a compliant Deployment. What
it cannot see is a SET that improves one file and costs points somewhere else without any single file
dropping a property of its own - a rewritten workflow that no longer builds the Dockerfile, where
`ci_builds_an_existing_dockerfile` stops holding and no per-file rule notices.

The score is the number the operator is watching, so the score is what has to be defended.
"""

from __future__ import annotations

from src.core.target_checks import score_delta, score_lowering_findings

PATH = "k8s/deployment.yaml"

COMPLIANT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: app
  template:
    metadata:
      labels:
        app: app
    spec:
      containers:
        - name: app
          image: app:1.2.3
          resources:
            requests: {cpu: "100m", memory: "128Mi"}
            limits: {cpu: "500m", memory: "512Mi"}
          livenessProbe: {httpGet: {path: /, port: 8000}}
          readinessProbe: {httpGet: {path: /, port: 8000}}
"""

DEGRADED = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  replicas: 1
  selector:
    matchLabels:
      app: app
  template:
    metadata:
      labels:
        app: app
    spec:
      containers:
        - name: app
          image: app:latest
"""


class TestTheScoreMayNotFall:
    def test_a_degrading_set_is_refused_and_names_the_checks_and_the_delta(self) -> None:
        findings = score_lowering_findings({PATH: DEGRADED}, {PATH: COMPLIANT})

        assert len(findings) == 1, findings
        message = findings[0]
        # The delta, so an operator can see the size of the loss without recomputing it.
        assert "lowers the readiness score from" in message
        # And the specific checks, because "the score fell" is not actionable on its own.
        assert "kubernetes_probes_declared falls from 25 to 0" in message
        assert "kubernetes_resource_limits_declared falls from 35 to 0" in message

    def test_an_improving_set_is_not_refused(self) -> None:
        """The guard must not stand in the way of the thing it exists to protect."""
        before, after, _ = score_delta({PATH: COMPLIANT}, {PATH: DEGRADED})

        assert after > before
        assert score_lowering_findings({PATH: COMPLIANT}, {PATH: DEGRADED}) == ()

    def test_an_unchanged_set_is_not_refused(self) -> None:
        assert score_lowering_findings({PATH: COMPLIANT}, {PATH: COMPLIANT}) == ()

    def test_a_first_generation_into_an_unindexed_project_is_not_refused(self) -> None:
        """There is no baseline to fall from, and inventing a zero one would say nothing.

        Reporting every artifact as an improvement over an empty repository is true and useless, and it
        would make the finding's number meaningless the one time it mattered.
        """
        assert score_lowering_findings({PATH: DEGRADED}, None) == ()
        assert score_lowering_findings({PATH: DEGRADED}, {}) == ()

    def test_the_refusal_is_not_path_prefixed_so_the_whole_set_is_withheld(self) -> None:
        """D-106 withholds per file; this is a property of the set taken together.

        Withholding one artifact from a set that lowers the score could leave a partial application that
        is worse than either the original or the whole set, so the set is refused entire.
        """
        findings = score_lowering_findings({PATH: DEGRADED}, {PATH: COMPLIANT})

        assert not findings[0].startswith(f"{PATH}: ")


class TestTheGateAppliesIt:
    @staticmethod
    def _service():
        from src.generation.service import GenerationService

        return GenerationService.__new__(GenerationService)

    @staticmethod
    def _file(path: str, content: str):
        from src.generation.service import GeneratedFile

        return GeneratedFile(path=path, content=content)

    def test_the_deterministic_gate_refuses_a_score_lowering_change_set(self) -> None:
        passed, findings = self._service()._validate([self._file(PATH, DEGRADED)], {PATH: COMPLIANT})

        assert passed is False
        assert any("lowers the readiness score" in f for f in findings), findings

    def test_the_gate_accepts_a_set_that_raises_the_score(self) -> None:
        passed, findings = self._service()._validate([self._file(PATH, COMPLIANT)], {PATH: DEGRADED})

        assert passed is True, findings
