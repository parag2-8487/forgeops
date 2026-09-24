# SPDX-License-Identifier: FSL-1.1-ALv2
"""A repair asks for ONE artifact, not the whole plan again. Phase 2 2.3.

THE MEASUREMENT THAT PRODUCED THIS
----------------------------------
`qwen2.5-coder:7b`, one Node repository, the real readiness gate, two shapes of ask:

    ASK A   6 artifacts, 55 mechanical requirements   FAIL, FAIL, FAIL
    ASK B   1 artifact,  24 mechanical requirements   FAIL, PASS

In ASK A the finding `dockerfile_base_pinned` was fed back twice and the model reproduced the same
`FROM node:$NODE_VERSION` each time. In ASK B the identical finding landed on the first correction and
the answer became `FROM node:22-slim`. Every other requirement was satisfied in both asks -- two stages,
`USER`, `HEALTHCHECK` -- so the variable is not capability. **What a wide ask costs is the model's
ability to act on a correction while regenerating five unrelated files.**

WHAT THESE TESTS ASSERT, AND WHY NOT THE LOOP
---------------------------------------------
`max_write_targets` counted the wrong thing for a long time underneath a test that watched its loop. So
nothing here inspects the retry mechanism. Each test reads WHAT THE MODEL WAS ACTUALLY SENT -- the
recorded prompts -- and what came out the other end. A stub model records every prompt it receives, which
is the only evidence that the ask was narrowed.
"""

from __future__ import annotations

import uuid

import pytest
from src.core.index_evidence import IndexEvidence
from src.core.model_port import ModelCompletion
from src.core.readiness import ReadinessEngine
from src.generation.prompt_compiler import compile_prompt, compile_repair_prompts
from src.generation.service import GenerationOutcome, GenerationService

pytestmark = pytest.mark.asyncio

PATHS = ("server.js", "package.json", "README.md")
CONTENTS = {
    "server.js": "const http = require('http');\nhttp.createServer((q, r) => r.end('ok')).listen(3000);\n",
    "package.json": '{\n  "name": "checkout",\n  "main": "server.js"\n}\n',
    "README.md": "# checkout\n",
}
INVENTORY = {
    "languages": ["javascript"],
    "package_managers": ["npm"],
    "entry_points": ["server.js"],
    "file_count": 3,
}
PROJECT = {"name": "checkout", "settings": {}}

# Refused: a floating base image. Everything else about it is correct, so the gate names exactly one
# artifact and the repair target is unambiguous.
BAD_DOCKERFILE = """### FILE: Dockerfile
```
FROM node:latest AS builder
WORKDIR /app
COPY package.json .
RUN npm install

FROM node:latest
WORKDIR /app
COPY --from=builder /app /app
USER 10001
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD node -e "process.exit(0)"
CMD ["node", "server.js"]
```
"""

GOOD_DOCKERFILE = BAD_DOCKERFILE.replace("node:latest", "node:22-slim")

# The audited floor's own manifests, captured from a real run. They pass the gate by definition --
# that is what makes the floor a floor -- so an attempt containing these plus a bad Dockerfile gives
# the loop something CORRECT to carry forward, which is the condition the carry-forward test needs.
# Inventing YAML here would make a failure ambiguous between a wrong fix and wrong YAML.
PASSING_MANIFESTS = """### FILE: k8s/deployment.yaml
```
apiVersion: apps/v1
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
        runAsUser: 1001
      containers:
        - name: app
          image: checkout-api:0.1.0
          ports:
            - containerPort: 8000
          # Requests are what the scheduler reserves; limits are what stops this container
          # taking the node. Tune both to the workload's measured usage.
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          # Without probes a wedged container keeps receiving traffic, because nothing asks it.
          livenessProbe:
            httpGet:
              path: /
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 20
          readinessProbe:
            httpGet:
              path: /
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - "ALL"
```
### FILE: k8s/service.yaml
```
apiVersion: v1
kind: Service
metadata:
  name: checkout-api
  labels:
    app: checkout-api
spec:
  type: ClusterIP
  selector:
    app: checkout-api
  ports:
    - name: http
      port: 80
      targetPort: 8000
      protocol: TCP
```
### FILE: k8s/ingress.yaml
```
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: checkout-api
  labels:
    app: checkout-api
spec:
  rules:
    - host: checkout-api.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: checkout-api
                port:
                  number: 80
```
"""


def _plan():
    report = ReadinessEngine().evaluate(IndexEvidence(paths=PATHS, contents=dict(CONTENTS)))
    compiled = compile_prompt(
        checks=report.checks,
        paths=PATHS,
        contents=dict(CONTENTS),
        inventory=INVENTORY,
        project_name="checkout",
    )
    repairs = compile_repair_prompts(
        checks=report.checks,
        paths=PATHS,
        contents=dict(CONTENTS),
        inventory=INVENTORY,
        project_name="checkout",
    )
    return compiled, repairs


class _Recorder:
    """Records every prompt, and answers differently on the repair so the loop can converge."""

    tier_name = "self_hosted"

    def __init__(self, answers: list[str]) -> None:
        self._answers = answers
        self.prompts: list[str] = []

    async def complete(  # noqa: ANN201
        self,
        *,
        prompt,
        on_token=None,
        may_serve_from_cache=True,
        store_in_cache=True,  # noqa: ANN001
    ):
        self.prompts.append(str(prompt))
        body = self._answers[min(len(self.prompts) - 1, len(self._answers) - 1)]
        return ModelCompletion(ok=True, served_from="provider", content=body, endpoint_id="stub-1")

    async def remember(self, *, prompt, content) -> None:  # noqa: ANN001
        return None


async def _run(answers: list[str], attempts: int = 3):
    compiled, repairs = _plan()
    model = _Recorder(answers)
    outcome = GenerationOutcome(run_id=uuid.uuid4())
    service = GenerationService(model=model, max_attempts=attempts)
    async for _ in service.stream_generation(
        uuid.uuid4(),
        "a node checkout service",
        outcome=outcome,
        project=PROJECT,
        compiled=compiled,
        existing=dict(CONTENTS),
        repair_prompts=repairs,
    ):
        pass
    return outcome, model, compiled, repairs


class TestTheOpeningAskIsUnchanged:
    async def test_the_first_prompt_is_the_whole_plan(self) -> None:
        """Deliberately not narrowed. It produces most of the set in one round, it is the cache key of
        the run, and nothing measured argues against it."""
        _, model, compiled, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])

        assert model.prompts, "the model was never called"
        first = model.prompts[0]
        # Every write target of the plan is named in the opening ask.
        for target in compiled.write_targets:
            assert target in first, f"the opening ask omits {target}"


class TestTheRepairAskIsNarrowed:
    async def test_the_repair_names_the_rejected_artifact_and_not_its_siblings(self) -> None:
        """THE FIX, asserted on what was SENT.

        The second prompt must be the Dockerfile's own instruction. If it still lists the CI workflow and
        the manifests, the ask was not narrowed and the measured failure mode is intact.
        """
        _, model, compiled, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])

        assert len(model.prompts) >= 2, f"expected a repair call; the model was called {len(model.prompts)} time(s)"
        repair = model.prompts[1]
        assert "Dockerfile" in repair

        siblings = [t for t in compiled.write_targets if t != "Dockerfile"]
        still_asked = [t for t in siblings if t in repair]
        assert still_asked == [], (
            f"the repair still asks for {still_asked}. Measured: asking for the siblings again is what "
            f"stopped the model acting on the correction."
        )

    async def test_the_repair_is_a_smaller_ask_than_the_opening_one(self) -> None:
        """The quantity that matters, stated as a number rather than implied."""
        _, model, _, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])

        def bullets(text: str) -> int:
            return sum(1 for line in text.splitlines() if line.strip().startswith("- "))

        opening, repair = bullets(model.prompts[0]), bullets(model.prompts[1])
        assert repair < opening, f"repair carries {repair} requirements, opening carried {opening}"

    async def test_only_the_rejected_artifacts_findings_are_fed_back(self) -> None:
        """Handing over five other files' complaints is the wide ask again, in the place it harms most."""
        _, model, _, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])

        repair = model.prompts[1]
        if "WHAT THE VALIDATORS SAID" not in repair:
            pytest.fail("the repair prompt carries no findings, so the model is told nothing to fix")
        tail = repair.split("WHAT THE VALIDATORS SAID", 1)[1]
        # Every fed-back finding is about the artifact being repaired.
        quoted = [line for line in tail.splitlines() if line.strip().startswith("- ")]
        assert quoted, "the findings section is empty"
        assert all("Dockerfile" in line for line in quoted), quoted


class TestWorkThatPassedIsNotThrownAway:
    async def test_the_delivered_set_keeps_artifacts_from_the_earlier_attempt(self) -> None:
        """A repair answer contains ONE file. Without carrying the rest forward, a successful repair
        would deliver a change set with a single artifact -- which is how a run that produced most of
        what it asked for came to be recorded as a fallback."""
        outcome, _, _, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])

        delivered = {artifact.path for artifact in outcome.files}
        assert "Dockerfile" in delivered
        assert len(delivered) > 1, (
            f"only {delivered} was delivered, so the repair discarded what the opening ask got right"
        )

    async def test_the_repaired_artifact_is_the_repaired_version(self) -> None:
        """The new answer wins for the path it covers. Carrying forward must not shadow the fix."""
        outcome, _, _, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])

        dockerfile = next(a for a in outcome.files if a.path == "Dockerfile")
        assert "node:22-slim" in dockerfile.content
        assert "node:latest" not in dockerfile.content


class TestTheDeliveredOrderIsThePlansOrder:
    """Order is a property of the PLAN, not of the model or of which attempt produced what.

    It was neither. A whole-plan answer arrived in whatever order the model wrote, and once repairs
    existed the carried artifacts would have come first with the repaired one last. Both orders reach
    `change_items.ordinal`, so the diff a reviewer reads was sequenced by something nobody chose and
    `ORDER BY ordinal` gave different answers for identical inputs.
    """

    async def test_the_order_follows_the_write_targets(self) -> None:
        outcome, _, compiled, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])

        delivered = [artifact.path for artifact in outcome.files]
        expected = [path for path in compiled.write_targets if path in set(delivered)]
        assert delivered == expected, f"delivered {delivered}, but the plan asked in the order {expected}"

    async def test_a_repair_does_not_move_the_repaired_artifact(self) -> None:
        """The specific regression a repair introduces: the fixed file landing last."""
        outcome, _, compiled, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])

        delivered = [artifact.path for artifact in outcome.files]
        if len(delivered) < 2:
            pytest.fail(f"only {delivered} was delivered, so ordering proves nothing")
        # `Dockerfile` is first in the plan, and it is the artifact that was repaired.
        assert delivered[0] == "Dockerfile", delivered

    async def test_the_same_inputs_give_the_same_order_twice(self) -> None:
        """Reproducible, which is the point: an ordinal that varies run to run is not an ordinal."""
        first, _, _, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])
        second, _, _, _ = await _run([BAD_DOCKERFILE + PASSING_MANIFESTS, GOOD_DOCKERFILE])

        assert [a.path for a in first.files] == [a.path for a in second.files]
