# SPDX-License-Identifier: FSL-1.1-ALv2
"""A run that served templates must say which attempt failed, why, and how long it ran. Phase 2 2.3.

WHY THIS EXISTS, AND WHAT IT WOULD HAVE SAVED
---------------------------------------------
The criterion-10 journey recorded `template_fallback` with `iterations_used=0`. Three investigations went
after it. The compiled prompt was checked and was correct. The index was checked and was complete. The
bytes sent by the route and by the service were compared and are identical. The service path, driven on the
same fixture with the same prompt, was ACCEPTED -- twice.

The cause was a TIMEOUT. `model_http_timeout_seconds` defaults to 300s; a real `qwen2.5-coder:7b` attempt
on this CPU measured 155s, 157s, 165s, 285s and 817s, and the journey's generation step ran 19.2 minutes
over three attempts -- about 384s each. Every attempt was aborted mid-flight and recorded as a refusal.
**That is D-104 again, on the budget introduced to fix D-104.**

None of that was visible in the row. A refusal and a truncation are the same row unless the DURATION is
recorded beside the reason, and the SSE stream that carried the detail was gone the moment the request
ended -- so the answer was unavailable for every run that had already happened.

These tests assert the row can now answer it. They use a stub whose failure is a real client timeout, so
the recorded reason is the one production would carry.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from src.core.model_port import ModelCompletion
from src.generation.service import GenerationOutcome, GenerationService

pytestmark = pytest.mark.asyncio

PROJECT = {"name": "checkout-api", "settings": {}}


class _TimesOut:
    """Fails the way a client budget does: after a measurable delay, with a timeout's words.

    Not an instant refusal. The delay is the subject -- a verdict reached in milliseconds and one reached
    at the exact client budget are different events, and the row has to tell them apart.
    """

    tier_name = "self_hosted"

    def __init__(self, delay_seconds: float = 0.25) -> None:
        self._delay = delay_seconds
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
        await asyncio.sleep(self._delay)
        return ModelCompletion(
            ok=False,
            failure_reasons=("the model call raised ReadTimeout: timed out after 300s",),
        )

    async def remember(self, *, prompt, content) -> None:  # noqa: ANN001
        return None


class _RefusesInstantly:
    """The control: a genuine refusal, reached immediately."""

    tier_name = "self_hosted"

    def __init__(self) -> None:
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
        return ModelCompletion(ok=False, failure_reasons=("every endpoint in the tier chain was skipped",))

    async def remember(self, *, prompt, content) -> None:  # noqa: ANN001
        return None


async def _run(model, attempts: int = 2) -> GenerationOutcome:
    outcome = GenerationOutcome(run_id=uuid.uuid4())
    async for _ in GenerationService(model=model, max_attempts=attempts).stream_generation(
        uuid.uuid4(), "a python checkout service", outcome=outcome, project=PROJECT
    ):
        pass
    return outcome


class TestEveryAttemptIsRecorded:
    async def test_a_template_fallback_names_its_attempts(self) -> None:
        """The gap this closes. `iterations_used=0` and `served_from='template'` said nothing about why."""
        model = _TimesOut()
        outcome = await _run(model)

        assert outcome.served_from == "template", outcome.served_from
        assert model.calls == 2, f"the model was called {model.calls} time(s)"
        assert len(outcome.attempts) == 2, (
            f"the run made {model.calls} attempts and recorded {len(outcome.attempts)}; a fallback that "
            f"cannot name its attempts is the row that cost three investigations"
        )

    async def test_each_record_carries_the_reason(self) -> None:
        outcome = await _run(_TimesOut())

        for record in outcome.attempts:
            assert record["verdict"] == "no_content", record
            assert "ReadTimeout" in str(record["reason"]), record

    async def test_each_record_carries_its_duration(self) -> None:
        """THE FIELD THAT DISTINGUISHES A TRUNCATION FROM A VERDICT.

        A refusal at the exact client budget is a truncation; the same words at 0.0s are a refusal. Without
        the duration those are one row.
        """
        outcome = await _run(_TimesOut(delay_seconds=0.25))

        for record in outcome.attempts:
            assert isinstance(record["seconds"], int | float), record
            assert float(record["seconds"]) >= 0.2, (
                f"a call that slept 0.25s recorded {record['seconds']}s, so the duration is not being "
                f"measured and a truncation cannot be told from a refusal"
            )

    async def test_a_fast_refusal_is_visibly_different(self) -> None:
        """NON-VACUITY. If `seconds` were a constant, or always large, it would prove nothing."""
        slow = await _run(_TimesOut(delay_seconds=0.25))
        fast = await _run(_RefusesInstantly())

        slowest = max(float(r["seconds"]) for r in slow.attempts)
        quickest = min(float(r["seconds"]) for r in fast.attempts)
        assert quickest < slowest, (
            f"an instant refusal recorded {quickest}s and a 0.25s truncation recorded {slowest}s; the "
            f"field is not tracking real elapsed time"
        )

    async def test_the_attempt_numbers_are_the_real_ones(self) -> None:
        outcome = await _run(_TimesOut(), attempts=3)
        assert [r["attempt"] for r in outcome.attempts] == [1, 2, 3]


class TestAnAcceptedRunIsRecordedToo:
    """Not only failures: a run that worked must be able to say how long its accepted attempt took, which
    is what makes the NFR-04 latency evidence readable from the row rather than from a stopwatch."""

    async def test_the_accepted_attempt_is_recorded(self) -> None:
        accepted = """### FILE: Dockerfile
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
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD python -c "import sys; sys.exit(0)"
CMD ["python", "-m", "app.main"]
```
### FILE: k8s/deployment.yaml
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

        class _Good:
            tier_name = "self_hosted"

            async def complete(  # noqa: ANN201
                self,
                *,
                prompt,
                on_token=None,
                may_serve_from_cache=True,
                store_in_cache=True,  # noqa: ANN001
            ):
                return ModelCompletion(ok=True, served_from="provider", content=accepted, endpoint_id="stub-1")

            async def remember(self, *, prompt, content) -> None:  # noqa: ANN001
                return None

        outcome = await _run(_Good())

        assert outcome.attempts, "an accepted run recorded no attempts at all"
        verdicts = [r["verdict"] for r in outcome.attempts]
        # The floor may substitute, which is its own verdict; what must not happen is silence.
        assert verdicts[-1] in {"accepted", "gate_refused"}, verdicts
