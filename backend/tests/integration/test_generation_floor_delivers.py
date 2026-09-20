# SPDX-License-Identifier: FSL-1.1-ALv2
"""The journey's step 8, reproduced through the real cascade and then fixed.

WHAT STEP 8 ASSERTS, AND WHY IT BELONGS HERE TOO. `expect(paths).toContain("Dockerfile")` — the change
set must carry the artifact §12.6 asked for. It failed against a real model whose Dockerfile had an
unpinned `FROM`, and reproducing that needs no model: the property is about what the cascade does with a
completion whose Dockerfile fails a target check, so the completion is supplied directly here. That makes
this a fast test of the same defect the 15-minute journey found, and the journey remains the proof that
the whole chain holds.

The model double returns the SAME unpinned Dockerfile every attempt, which is the realistic case: a 1.5B
model told "pin the base image" often returns the same bytes. The old behaviour delivered the siblings
and no Dockerfile. The new behaviour delivers the audited floor's Dockerfile, and it is asserted to pass
the very check that withheld the model's.
"""

from __future__ import annotations

import json
import uuid

import pytest
from src.core.model_port import ModelCompletion
from src.core.target_checks import unsatisfied_targets
from src.generation.service import GenerationService

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

UNPINNED_DOCKERFILE = """FROM node:latest
WORKDIR /app
COPY . .
EXPOSE 3000
USER 10001
HEALTHCHECK --interval=30s CMD node -e "process.exit(0)"
CMD ["node", "server.js"]
"""

GOOD_SERVICE = """apiVersion: v1
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


GOOD_DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout-api
  labels:
    app: checkout-api
spec:
  replicas: 1
  selector:
    matchLabels:
      app: checkout-api
  template:
    metadata:
      labels:
        app: checkout-api
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
      containers:
        - name: app
          image: checkout-api:0.1.0
          ports:
            - containerPort: 3000
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 256Mi
          livenessProbe:
            httpGet:
              path: /
              port: 3000
          readinessProbe:
            httpGet:
              path: /
              port: 3000
"""

GOOD_INGRESS = """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: checkout-api
spec:
  rules:
    - host: checkout-api.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: checkout-api
                port:
                  number: 80
"""


def _completion_body() -> str:
    """The four required artifacts in the wire shape `parse_artifacts` reads.

    THREE ARE GOOD AND ONE FAILS ITS TARGET CHECK, which is the whole point. An answer MISSING a required
    artifact is a parse failure and falls through to the template path — so the first version of this
    stub, which returned two files, passed three of these assertions for entirely the wrong reason: the
    template path delivers a Dockerfile too. That is the same class of mistake this file exists to pin,
    made inside the test for it, and it is why `served_from` is asserted below.
    """
    return "\n".join(
        [
            "FILE: Dockerfile",
            "```dockerfile",
            UNPINNED_DOCKERFILE.rstrip("\n"),
            "```",
            "",
            "FILE: k8s/deployment.yaml",
            "```yaml",
            GOOD_DEPLOYMENT.rstrip("\n"),
            "```",
            "",
            "FILE: k8s/service.yaml",
            "```yaml",
            GOOD_SERVICE.rstrip("\n"),
            "```",
            "",
            "FILE: k8s/ingress.yaml",
            "```yaml",
            GOOD_INGRESS.rstrip("\n"),
            "```",
            "",
        ]
    )


class _ModelReturningAnUnpinnedDockerfile:
    """Answers every attempt identically, which is what the real 1.5B model did."""

    tier_name = "self_hosted"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, prompt, on_token=None):  # noqa: ANN001, ANN003
        self.calls += 1
        content = _completion_body()
        if on_token is not None:
            on_token(content)
        return ModelCompletion(
            ok=True,
            served_from="provider",
            content=content,
            endpoint_id="stub",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            streamed=True,
        )


async def _run(service: GenerationService) -> tuple[list[str], list[dict], str]:
    """Drive the real stream and return the delivered paths, validation frames and `served_from`."""
    frames: list[dict] = []
    delivered: list[str] = []
    served_from = ""
    async for raw in service.stream_generation(
        uuid.uuid4(),
        "Generate a Dockerfile and Kubernetes manifests for this Node.js service.",
        project={"name": "checkout-api", "settings": {"runtime": "node", "port": 3000}},
    ):
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[len("data:") :].strip())
            if "passed" in payload:
                frames.append(payload)
            if payload.get("state") == "accepted":
                delivered = list(payload.get("files") or [])
                served_from = str(payload.get("served_from") or "")
    return delivered, frames, served_from


class TestTheTestItselfIsExercisingTheRightPath:
    async def test_the_run_is_served_from_the_provider_and_not_the_template_path(self) -> None:
        """GUARDS EVERY ASSERTION BELOW.

        If the stubbed completion stops parsing, the cascade falls through to the template path, which
        delivers a Dockerfile of its own — and every assertion in this file would pass while testing
        nothing. That happened on the first draft. This is the assertion that catches it.
        """
        _, _, served_from = await _run(GenerationService(model=_ModelReturningAnUnpinnedDockerfile()))

        assert served_from == "provider", served_from


class TestAFailingDockerfileIsReplacedRatherThanLost:
    async def test_the_change_set_carries_a_dockerfile(self) -> None:
        """Step 8's assertion, through the cascade that dropped it."""
        model = _ModelReturningAnUnpinnedDockerfile()

        delivered, _, _ = await _run(GenerationService(model=model))

        assert "Dockerfile" in delivered, delivered
        assert "k8s/service.yaml" in delivered, delivered

    async def test_the_model_exhausted_its_attempts_first(self) -> None:
        """The substitution is a LAST resort: the model is asked to repair its own output first."""
        model = _ModelReturningAnUnpinnedDockerfile()

        await _run(GenerationService(model=model))

        assert model.calls == 3, model.calls

    async def test_the_delivered_dockerfile_satisfies_the_check_that_withheld_the_model_s(self) -> None:
        model = _ModelReturningAnUnpinnedDockerfile()
        service = GenerationService(model=model)

        delivered, _, _ = await _run(service)
        assert "Dockerfile" in delivered

        # The floor's Dockerfile for these inputs, which is what the run delivered.
        floor = {
            item.path: item.content
            for item in service._render(
                "Generate a Dockerfile and Kubernetes manifests for this Node.js service.",
                {"name": "checkout-api", "settings": {"runtime": "node", "port": 3000}},
            )
        }
        assert "node:latest" not in floor["Dockerfile"]
        assert unsatisfied_targets({"Dockerfile": floor["Dockerfile"]}) == ()

    async def test_the_substitution_is_reported_rather_than_silent(self) -> None:
        """An operator approving the change set must be able to see the floor was used."""
        model = _ModelReturningAnUnpinnedDockerfile()

        _, frames, _ = await _run(GenerationService(model=model))

        said = [
            finding for frame in frames for finding in frame.get("findings", []) if "template floor replaced" in finding
        ]
        assert said, frames
        assert "Dockerfile" in said[0]
