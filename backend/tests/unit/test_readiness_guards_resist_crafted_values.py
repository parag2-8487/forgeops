# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every readiness guard, against a value crafted to defeat it. Phase 2 2.3.

WHY THIS FILE EXISTS
--------------------
`dockerfile_base_pinned` accepted `FROM node:$NODE_VERSION`, because the guard was
`image.startswith("$")` and here the `$` is in the tag. It was found by reading what SATISFIED the check
rather than what failed it. **A check that can be satisfied by the thing it exists to forbid is worse than
no check, because the score claims the property and nobody is looking.**

That is a category, not an incident, and sweeping it found six more holes in five checks:

    dockerfile_healthcheck          accepted `HEALTHCHECK NONE` -- Docker's syntax for DISABLING one
    kubernetes_image_tags_pinned    accepted `image: app:${TAG}` -- the same defect, k8s twin
    kubernetes_resource_limits      accepted `cpu: ""`, `cpu: null` and `cpu: "0"`
    kubernetes_probes               accepted `livenessProbe: {}` -- refused by the API server
    pipeline_runs_tests             accepted a commented-out `# npm test`

The classes, which is what makes this a sweep rather than six fixes:

  * INTERPOLATION anywhere in a value, not only at its start.
  * An EMPTY RESOLUTION: syntactically present, semantically absent.
  * A value that PARSES AND MEANS NOTHING -- a zero limit is the absence of a limit written as a number.
  * A PRESENCE TEST where a CONTENT test was meant -- a key with no usable value under it.
  * A SUBSTRING MATCH where a whole-line or executed-line match was meant.

Every row below is a value a repository could really contain. None is malformed: each one parses, and a
human skim-reading the file would believe the property held. That is the point -- a malformed value is
caught by the parser and needs no guard.

EACH ROW ALSO HAS ITS POSITIVE CONTROL. Refusing everything would satisfy a table of refusals and destroy
the checks, so every group asserts the honest value is still accepted.
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

K8S_PATHS = ("k8s/deployment.yaml",)
WF_PATHS = (".github/workflows/ci.yml",)

CONTAINER = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  template:
    spec:
      containers:
        - name: c
{extra}
"""

WORKFLOW = """name: ci
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
{steps}
"""


def _k8s(extra: str) -> dict[str, str]:
    return {"k8s/deployment.yaml": CONTAINER.format(extra=extra)}


def _wf(steps: str) -> dict[str, str]:
    return {".github/workflows/ci.yml": WORKFLOW.format(steps=steps)}


class TestABuildArgumentNeverCountsAsAPin:
    """The original hole, and the two shapes either side of it."""

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("a variable as the tag", "FROM node:$V\nUSER 10001\n"),
            ("a braced variable as the tag", "FROM node:${V}\nUSER 10001\n"),
            ("a variable as the registry", "FROM $REG/node:22\nUSER 10001\n"),
            ("a variable as the whole image", "FROM $IMAGE\nUSER 10001\n"),
            ("an empty tag", "FROM node:\nUSER 10001\n"),
        ],
    )
    def test_it_is_refused(self, label: str, body: str) -> None:
        assert dockerfile_base_pinned(body) is False, label

    def test_a_real_pin_is_accepted(self) -> None:
        assert dockerfile_base_pinned("FROM node:22-slim\nUSER 10001\n") is True
        assert dockerfile_base_pinned("FROM node@sha256:" + "a" * 64 + "\n") is True


class TestDisablingAHealthcheckIsNotDeclaringOne:
    """`HEALTHCHECK NONE` is the instruction that switches the property OFF, and it satisfied the check.

    An image built from such a Dockerfile has its inherited healthcheck REMOVED, so the score was not
    merely unproven -- it reported the opposite of the truth.
    """

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("NONE", "FROM node:22\nHEALTHCHECK NONE\n"),
            ("NONE lowercased", "FROM node:22\nhealthcheck none\n"),
            ("a comment", "FROM node:22\n# HEALTHCHECK matters\n"),
            ("the word inside a command", 'FROM node:22\nRUN echo "HEALTHCHECK"\n'),
        ],
    )
    def test_it_is_refused(self, label: str, body: str) -> None:
        assert dockerfile_healthcheck(body) is False, label

    def test_a_real_healthcheck_is_accepted(self) -> None:
        assert dockerfile_healthcheck(
            "FROM node:22\nHEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD node -e 'process.exit(0)'\n"
        )


class TestASubstitutedImageTagIsNotPinned:
    """The k8s twin of the Dockerfile hole. Kustomize, Helm and envsubst all reach a cluster this way."""

    @pytest.mark.parametrize(
        ("label", "extra"),
        [
            ("braced substitution", "          image: app:${TAG}\n"),
            ("shell substitution", "          image: app:$(TAG)\n"),
            ("bare variable", "          image: app:$TAG\n"),
            ("substituted registry", "          image: ${REG}/app:1.2.3\n"),
            ("empty image", '          image: ""\n'),
            ("absent image", "          image:\n"),
        ],
    )
    def test_it_is_refused(self, label: str, extra: str) -> None:
        passed, _ = kubernetes_image_tags_pinned(K8S_PATHS, _k8s(extra))
        assert passed is False, label

    def test_a_real_tag_and_a_digest_are_accepted(self) -> None:
        assert kubernetes_image_tags_pinned(K8S_PATHS, _k8s("          image: app:1.2.3\n"))[0]
        assert kubernetes_image_tags_pinned(K8S_PATHS, _k8s("          image: app@sha256:" + "a" * 64 + "\n"))[0]


class TestADeclaredResourceIsNotABoundResource:
    """`cpu: ""` and `cpu: "0"` satisfied a membership test. Neither bounds anything, and the check exists
    so one container cannot evict its neighbours."""

    RESOURCES = "          image: app:1\n          resources:\n{r}"

    @pytest.mark.parametrize(
        ("label", "r"),
        [
            (
                "empty strings",
                '            requests: {cpu: "", memory: ""}\n            limits: {cpu: "", memory: ""}\n',
            ),
            (
                "nulls",
                "            requests: {cpu: null, memory: null}\n            limits: {cpu: null, memory: null}\n",
            ),
            (
                "zeroes",
                '            requests: {cpu: "0", memory: "0"}\n            limits: {cpu: "0", memory: "0"}\n',
            ),
            (
                "zero with a unit",
                '            requests: {cpu: "0m", memory: "0Mi"}\n            limits: {cpu: "0m", memory: "0Mi"}\n',
            ),
            ("empty maps", "            requests: {}\n            limits: {}\n"),
        ],
    )
    def test_it_is_refused(self, label: str, r: str) -> None:
        passed, _ = kubernetes_resource_limits(K8S_PATHS, _k8s(self.RESOURCES.format(r=r)))
        assert passed is False, label

    def test_real_quantities_are_accepted(self) -> None:
        passed, _ = kubernetes_resource_limits(
            K8S_PATHS,
            _k8s(
                self.RESOURCES.format(
                    r='            requests: {cpu: "100m", memory: "128Mi"}\n'
                    '            limits: {cpu: "500m", memory: "256Mi"}\n'
                )
            ),
        )
        assert passed is True


class TestAProbeMustSayHowToProbe:
    """`livenessProbe: {}` is a Mapping, and the API server refuses it -- so the manifest does not deploy
    while the score reported the workload as probed."""

    @pytest.mark.parametrize(
        ("label", "extra"),
        [
            ("empty maps", "          image: app:1\n          livenessProbe: {}\n          readinessProbe: {}\n"),
            ("nulls", "          image: app:1\n          livenessProbe:\n          readinessProbe:\n"),
            (
                "handlers present but empty",
                "          image: app:1\n"
                "          livenessProbe:\n            httpGet:\n"
                "          readinessProbe:\n            httpGet:\n",
            ),
            (
                "only one of the two",
                "          image: app:1\n          livenessProbe:\n            httpGet: {path: /h, port: 3000}\n",
            ),
        ],
    )
    def test_it_is_refused(self, label: str, extra: str) -> None:
        passed, _ = kubernetes_probes(K8S_PATHS, _k8s(extra))
        assert passed is False, label

    def test_real_probes_are_accepted(self) -> None:
        passed, _ = kubernetes_probes(
            K8S_PATHS,
            _k8s(
                "          image: app:1\n"
                "          livenessProbe:\n            httpGet: {path: /healthz, port: 3000}\n"
                "          readinessProbe:\n            tcpSocket: {port: 3000}\n"
            ),
        )
        assert passed is True


class TestACommentedCommandIsNotACommand:
    """A pipeline whose test line was commented out reported as running tests, and a pipeline with no
    tests is green for every change."""

    @pytest.mark.parametrize(
        ("label", "steps"),
        [
            ("commented in a block", "      - run: |\n          # npm test\n          echo built\n"),
            ("trailing comment only", "      - run: echo built # npm test\n"),
            ("named test, does nothing", "      - name: test suite\n        run: echo hi\n"),
        ],
    )
    def test_it_is_refused(self, label: str, steps: str) -> None:
        passed, _ = pipeline_runs_tests(WF_PATHS, _wf(steps))
        assert passed is False, label

    def test_a_real_test_command_is_accepted(self) -> None:
        assert pipeline_runs_tests(WF_PATHS, _wf("      - run: npm test\n"))[0]
        assert pipeline_runs_tests(WF_PATHS, _wf("      - run: |\n          npm ci\n          npm test\n"))[0]


class TestTheGuardsAlreadySoundStaySound:
    """Recorded rather than assumed. These two were swept with the same crafted values and held, so the
    sweep's conclusion is "five of seven had holes", not "five had holes and two were not looked at"."""

    @pytest.mark.parametrize(
        ("label", "steps"),
        [
            ("an interpolated ref", "      - uses: actions/checkout@${{ env.SHA }}\n"),
            ("an empty ref", "      - uses: actions/checkout@\n"),
            ("forty non-hex characters", "      - uses: actions/checkout@" + "z" * 40 + "\n"),
            ("a tag", "      - uses: actions/checkout@v4\n"),
        ],
    )
    def test_action_pinning_refuses_it(self, label: str, steps: str) -> None:
        passed, _ = pipeline_actions_pinned(WF_PATHS, _wf(steps))
        assert passed is False, label

    def test_action_pinning_accepts_a_real_sha(self) -> None:
        assert pipeline_actions_pinned(WF_PATHS, _wf("      - uses: actions/checkout@" + "a" * 40 + "\n"))[0]

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("jobs declared empty", "name: ci\non: [push]\njobs: {}\n"),
            ("a job with null steps", "name: ci\non: [push]\njobs:\n  b:\n    runs-on: x\n    steps:\n"),
        ],
    )
    def test_stage_declaration_refuses_it(self, label: str, body: str) -> None:
        passed, _ = pipeline_stages_declared(WF_PATHS, {".github/workflows/ci.yml": body})
        assert passed is False, label
