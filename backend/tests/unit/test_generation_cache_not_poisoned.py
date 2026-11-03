# SPDX-License-Identifier: FSL-1.1-ALv2
"""A run the template floor partly rescued must not populate the cache. Phase 2 2.3.

THE DEFECT, MEASURED END TO END ON THE REAL MODEL
-------------------------------------------------
`test_generation_run_rows.py::TestACacheHitStoresL1OrL2` asserted a repeated prompt is served `l1`
and got `provider`. Four layers were exonerated by measurement before anything was changed: the
compiled prompt is byte-identical across two runs on unchanged project state (no rows are added to
`file_tree`, `file_contents` or `change_sets`, so the stated "the index changed" hypothesis was
false), `remember` writes the key `lookup` asks for, a second port finds the first port's write, and
`router.complete` finds an entry `remember` wrote. Instrumenting the lookup on a real run then
reported:

    LOOKUP model='qwen2.5-coder:7b' params={'temperature': 0.0, 'max_tokens': 2048} -> L1_exact

**The cache hit happened.** The run still recorded `served_from='provider'` and `iterations_used=3`.
Run 1 had been delivered with the floor standing in for the artifacts the gate refused, and what it
cached was the RAW MODEL TEXT the gate had just refused. Run 2 hit that entry, had the content refused
for the same reason, burned its first attempt, and called the provider anyway.

That is the POISONED ENTRY the router's `store_in_cache=False` comment describes, arriving by the one
route that guard does not cover. It is strictly worse than not caching: the run pays the lookup,
receives a known-bad answer, spends an attempt rediscovering that it is bad, and still calls the
provider. An entry that can never serve is indistinguishable from a cache that is working and never
hitting, which is why it survived -- the hit rate was zero and every run looked correct.

WHY THE SCENARIO IS *PARTIAL* SUBSTITUTION
------------------------------------------
Not "the model failed": when NOTHING of the model's output passes, the floor is not applied at all and
the template path records `served_from='template'`, which never reaches the cache write. The defect
needs the mixed case -- some artifacts accepted, others substituted -- which is also the case that
keeps the model's provenance. The first draft of this file used total failure and would have passed
without the fix.

The manifests below are the FLOOR'S OWN, captured from a real run rather than invented, because the
floor's artifacts satisfy the gate by definition. Hand-written YAML would make a failure ambiguous
between a wrong fix and wrong YAML.
"""

from __future__ import annotations

import uuid

import pytest
from src.core.model_port import ModelCompletion
from src.generation.service import GenerationOutcome, GenerationService

pytestmark = pytest.mark.asyncio

PROJECT = {"name": "checkout-api", "settings": {}}

DOCKERFILE = """### FILE: Dockerfile
```
FROM python:3.13-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --target=/deps -r requirements.txt

FROM python:3.13-slim
WORKDIR /app
COPY --from=builder /deps /usr/local/lib/python3.13/site-packages
COPY . .
USER 10001
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import sys; sys.exit(0)"
CMD ["python", "-m", "app.main"]
```
"""

DEPLOYMENT = """### FILE: k8s/deployment.yaml
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
"""

SERVICE = """### FILE: k8s/service.yaml
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
"""

INGRESS = """### FILE: k8s/ingress.yaml
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

REFUSED_INGRESS = """### FILE: k8s/ingress.yaml
```
apiVersion: v1
kind: NotAnIngress
```
"""


class _Model:
    """Returns a fixed completion and records every `remember`."""

    tier_name = "self_hosted"

    def __init__(self, content: str) -> None:
        self._content = content
        self.remembered: list[str] = []
        self.calls = 0

    async def complete(  # noqa: ANN201
        self,
        *,
        prompt,
        on_token=None,
        may_serve_from_cache=True,
        store_in_cache=True,  # noqa: ANN001
    ):
        self.calls += 1
        return ModelCompletion(ok=True, served_from="provider", content=self._content, endpoint_id="stub-1")

    async def remember(self, *, prompt, content) -> None:  # noqa: ANN001
        self.remembered.append(str(content))


async def _run(content: str) -> tuple[GenerationOutcome, _Model]:
    model = _Model(content)
    outcome = GenerationOutcome(run_id=uuid.uuid4())
    async for _ in GenerationService(model=model).stream_generation(
        uuid.uuid4(), "a python checkout service", outcome=outcome, project=PROJECT
    ):
        pass
    return outcome, model


class TestAPartlySubstitutedRunDoesNotPopulateTheCache:
    """THE REGRESSION. Three artifacts accepted, one supplied by the floor."""

    async def test_the_scenario_is_the_one_that_was_measured(self) -> None:
        """GUARDS THE ASSERTION BELOW.

        The defect needs `served_from='provider'` with a substitution having occurred. If this run
        fell through to the template path instead, the next assertion would pass for the wrong reason
        -- which is exactly how the first draft of this file proved nothing.
        """
        outcome, model = await _run(DOCKERFILE + DEPLOYMENT + SERVICE + REFUSED_INGRESS)

        assert model.calls > 0, "the model was never called"
        assert outcome.served_from == "provider", (
            f"this run was served {outcome.served_from!r}, so it is not the mixed case the defect "
            f"needs and the assertion below would be vacuous"
        )
        assert outcome.files, "the run delivered nothing"

    async def test_nothing_is_remembered(self) -> None:
        """The fix. The raw completion is not what was accepted, so it is not what is kept."""
        _, model = await _run(DOCKERFILE + DEPLOYMENT + SERVICE + REFUSED_INGRESS)

        assert model.remembered == [], (
            "content that needed the floor was cached. Every later run with this prompt is served it, "
            "has it refused by the same gate, and burns an attempt before calling the provider."
        )


class TestAnUnaidedRunStillPopulatesTheCache:
    """THE NON-VACUITY CONTROL. Without it, deleting the `remember` call would satisfy everything
    above -- caching would be gone rather than corrected."""

    async def test_the_scenario_needed_no_floor(self) -> None:
        outcome, _ = await _run(DOCKERFILE + DEPLOYMENT + SERVICE + INGRESS)
        assert outcome.served_from == "provider", (
            f"the four artifacts no longer satisfy the gate unaided (served {outcome.served_from!r}), "
            f"so the control proves nothing. Fix the fixture, not the assertion."
        )

    async def test_content_that_passed_unaided_is_remembered(self) -> None:
        _, model = await _run(DOCKERFILE + DEPLOYMENT + SERVICE + INGRESS)
        assert model.remembered, "output the gate accepted unaided was not cached"
