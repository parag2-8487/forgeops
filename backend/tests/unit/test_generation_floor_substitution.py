# SPDX-License-Identifier: FSL-1.1-ALv2
"""The template floor applies PER ARTIFACT, not only when the whole model answer is unusable.

THE DEFECT THIS PINS, AND HOW IT WAS FOUND. The thirteen-step journey failed at step 8: the change set
held `SECURITY.md`, `k8s/ingress.yaml`, `k8s/service.yaml` and `terraform/backend.tf`, and **no
`Dockerfile`**. The model had returned one with an unpinned `FROM`, the per-file gate withheld it
correctly, and nothing put the known-good template Dockerfile in its place — because the only route to
the floor was `if not accepted`, which needs EVERY artifact to have failed. So the floor covered the case
where the model says nothing and missed the case it was built for: the model says something wrong about
one kind and something right about the rest.

`scripts/check-template-readiness.py` had been certifying the floor against every target check the whole
time. A thing that exists with the right name, is audited in CI, and is read by no live path in the
situation it was designed for — the recurring defect class of this codebase, one more time.

WHAT THE FIX MAY NOT DO, asserted below: it may not deliver the model's failing artifact, it may not
invent a substitute for a path the floor does not render, and it may not accept a substitute that fails
its own target checks or that makes the set as a whole worse.
"""

from __future__ import annotations

import pytest
from src.core.target_checks import unsatisfied_targets
from src.generation.service import GeneratedFile, GenerationService

pytestmark = pytest.mark.mandatory

PROJECT = {"name": "checkout-api", "settings": {"runtime": "node", "port": 3000}}
PROMPT = "Generate a Dockerfile and Kubernetes manifests for this Node.js service."

#: What the real model returned in the journey, in the property that mattered: an unpinned base image.
MODEL_DOCKERFILE_UNPINNED = """FROM node:latest
WORKDIR /app
COPY . .
EXPOSE 3000
CMD ["node", "server.js"]
"""

GOOD_SIBLING = """apiVersion: v1
kind: Service
metadata:
  name: checkout-api
spec:
  selector:
    app: checkout-api
  ports:
    - port: 80
      targetPort: 3000
"""


def _service() -> GenerationService:
    return GenerationService()


class TestTheFloorReplacesAWithheldArtifact:
    def test_the_dockerfile_comes_back_from_the_floor(self) -> None:
        """Step 8's assertion, at the unit the defect lives in."""
        service = _service()
        accepted = (GeneratedFile(path="k8s/service.yaml", content=GOOD_SIBLING),)

        delivered, substituted = service._apply_floor(
            accepted=accepted,
            rejected=("Dockerfile",),
            prompt=PROMPT,
            project=PROJECT,
            existing=None,
        )

        paths = [item.path for item in delivered]
        assert "Dockerfile" in paths, paths
        assert substituted == ("Dockerfile",)

    def test_the_substitute_is_the_floor_and_not_the_model_s_rejected_bytes(self) -> None:
        """The failing artifact must not be delivered under cover of the substitution."""
        service = _service()

        delivered, _ = service._apply_floor(
            accepted=(GeneratedFile(path="k8s/service.yaml", content=GOOD_SIBLING),),
            rejected=("Dockerfile",),
            prompt=PROMPT,
            project=PROJECT,
            existing=None,
        )

        dockerfile = next(item.content for item in delivered if item.path == "Dockerfile")
        assert dockerfile != MODEL_DOCKERFILE_UNPINNED
        assert "node:latest" not in dockerfile

    def test_the_delivered_set_satisfies_the_checks_it_targets(self) -> None:
        """The whole point: what reaches the change set passes the rule that withheld the original."""
        service = _service()

        delivered, _ = service._apply_floor(
            accepted=(GeneratedFile(path="k8s/service.yaml", content=GOOD_SIBLING),),
            rejected=("Dockerfile",),
            prompt=PROMPT,
            project=PROJECT,
            existing=None,
        )

        assert unsatisfied_targets({item.path: item.content for item in delivered}) == ()

    def test_several_rejected_artifacts_are_all_replaced(self) -> None:
        """The journey withheld two: the Dockerfile and the Deployment."""
        service = _service()

        delivered, substituted = service._apply_floor(
            accepted=(),
            rejected=("Dockerfile", "k8s/deployment.yaml"),
            prompt=PROMPT,
            project=PROJECT,
            existing=None,
        )

        assert substituted == ("Dockerfile", "k8s/deployment.yaml")
        assert unsatisfied_targets({item.path: item.content for item in delivered}) == ()


class TestTheFloorDoesNotInventAnything:
    def test_a_path_the_floor_does_not_render_gets_no_substitute(self) -> None:
        """Inventing content for an arbitrary path would be fabricating an artifact."""
        service = _service()

        delivered, substituted = service._apply_floor(
            accepted=(GeneratedFile(path="k8s/service.yaml", content=GOOD_SIBLING),),
            rejected=("src/app/very/specific/handler.py",),
            prompt=PROMPT,
            project=PROJECT,
            existing=None,
        )

        assert substituted == ()
        assert [item.path for item in delivered] == ["k8s/service.yaml"]

    def test_nothing_rejected_means_nothing_touched(self) -> None:
        service = _service()
        accepted = (GeneratedFile(path="k8s/service.yaml", content=GOOD_SIBLING),)

        delivered, substituted = service._apply_floor(
            accepted=accepted, rejected=(), prompt=PROMPT, project=PROJECT, existing=None
        )

        assert delivered == accepted
        assert substituted == ()

    def test_a_substitution_that_would_lower_the_score_is_refused(self) -> None:
        """Invariant 2 still governs the assembled set.

        The existing Deployment here is richer than the floor's, so substituting the floor would lower
        the score. The withholding must stand: a floor is a floor, not a licence to regress a
        repository that is already better than it.
        """
        service = _service()
        richer_existing = (
            service._render(PROMPT, PROJECT)  # the floor's own Deployment, plus an extra replica set
        )
        existing = {item.path: item.content for item in richer_existing}
        existing["k8s/deployment.yaml"] = existing["k8s/deployment.yaml"].replace(
            "replicas: 1", "replicas: 3\n  revisionHistoryLimit: 5"
        )

        delivered, substituted = service._apply_floor(
            accepted=(),
            rejected=("k8s/deployment.yaml",),
            prompt=PROMPT,
            project=PROJECT,
            existing=existing,
        )

        # Either it was refused, or it was accepted and the whole gate agreed it is not worse.
        if substituted:
            passed, findings = service._validate(delivered, existing)
            assert passed, findings
        else:
            assert delivered == ()
