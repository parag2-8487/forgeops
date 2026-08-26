# SPDX-License-Identifier: FSL-1.1-ALv2
"""`GenerationService` routes: cache -> provider -> cascade -> template (design §11.5, §13.7).

WHAT THESE ASSERT AND WHY IT DID NOT EXIST
------------------------------------------
`generation/routes.py` INSERTed `served_from` as the SQL string literal `'template'`, so the
template library was not the fallback — it was the only path, and the column could not have
recorded a provider call, an L1 hit or an L2 hit no matter what happened. `GenerationService` never
touched `ai/routing/` at all: a complete six-tier cascade with a cache, per-endpoint breakers and a
key resolver, whose only caller was an `/api/v1/ai/complete` route no product surface uses.

So every assertion here is about which path served the run and what the outcome records, because
that is the fact the schema was shaped to carry and nothing set.

The transport is `httpx.MockTransport` (a WIRE substitution, §0.4.1 permits it) so these run
without a model server and cover the branches a real model cannot be made to take on demand — a
500 from every endpoint, output that does not parse, output that fails the gate. The genuine
model call is `tests/integration/test_self_hosted_generation.py`, which needs the real thing.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from src.ai.generation_port import build_artifact_model
from src.ai.routing.breaker import CircuitBreaker
from src.ai.routing.cache import TieredSemanticCache
from src.ai.routing.endpoints import EndpointRegistry
from src.ai.routing.keys import EnvKeyResolver
from src.ai.routing.router import ModelRouter
from src.ai.routing.tiers import (
    EndpointDescriptor,
    EndpointProtocol,
    ModelTier,
    TierChain,
    TierConfig,
)
from src.core.model_port import SERVED_FROM as PORT_SERVED_FROM
from src.core.sse import SSEEventType
from src.generation.models import SERVED_FROM
from src.generation.service import GenerationOutcome, GenerationService

pytestmark = pytest.mark.asyncio

MODEL = "test-coder"

#: Model output that satisfies both the parse contract and §11.5.5's deterministic gate.
#:
#: A fixture, and it lives in a test file, which is where the standing rule puts one. It is written
#: out rather than generated so the assertions below turn on the PATH taken and not on whether a
#: generator happened to produce valid YAML.
GOOD_OUTPUT = """### FILE: Dockerfile
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8000
USER 1001
CMD ["python", "main.py"]
```

### FILE: k8s/deployment.yaml
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout-api
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
      containers:
        - name: app
          image: checkout-api:latest
          ports:
            - containerPort: 8000
```

### FILE: k8s/service.yaml
```yaml
apiVersion: v1
kind: Service
metadata:
  name: checkout-api
spec:
  type: ClusterIP
  selector:
    app: checkout-api
  ports:
    - port: 80
      targetPort: 8000
```

### FILE: k8s/ingress.yaml
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: checkout-api
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

#: The same four files, but the Dockerfile never drops root — the gate refuses it.
ROOTFUL_OUTPUT = GOOD_OUTPUT.replace("USER 1001\n", "")

PROJECT = {"name": "checkout-api", "path": "/tmp/checkout", "repo_url": None, "settings": {}}


class _Redis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.index: dict[str, dict[str, str]] = {}

    async def get(self, key: str) -> bytes | None:
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    async def set(self, key: str, value: str | bytes, *, ex: int | None = None) -> bool:
        self.store[key] = value.decode("utf-8") if isinstance(value, bytes) else value
        return True

    async def hset(self, name: str, key: str, value: str | bytes) -> int:
        payload = value.decode("utf-8") if isinstance(value, bytes) else value
        self.index.setdefault(name, {})[key] = payload
        return 1

    async def hgetall(self, name: str) -> dict[str, str]:
        return dict(self.index.get(name, {}))


def _sse_for(text: str, *, chunk: int = 64) -> bytes:
    """Split `text` into provider frames, so the deltas the service sees come off a wire."""
    frames = [
        {"model": MODEL, "choices": [{"index": 0, "delta": {"content": text[i : i + chunk]}}]}
        for i in range(0, len(text), chunk)
    ]
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    return (body + "data: [DONE]\n\n").encode("utf-8")


def _service(handler, *, redis: _Redis | None = None, embed=None, max_attempts: int = 3) -> GenerationService:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    descriptor = EndpointDescriptor(
        id="local",
        provider="self_hosted",
        model=MODEL,
        protocol=EndpointProtocol.OPENAI_COMPATIBLE,
        base_url="http://local.invalid/v1",
        key_ref=None,
    )
    config = TierConfig(
        tiers={ModelTier.SELF_HOSTED: TierChain(primary="local")},
        endpoints={"local": descriptor},
    )
    cache_kwargs = {"redis": redis or _Redis()}
    if embed is not None:
        cache_kwargs |= {"embed": embed, "similarity_threshold": 0.95}
    router = ModelRouter(
        tier_config=config,
        registry=EndpointRegistry.from_config(config, http=http),
        cache=TieredSemanticCache(**cache_kwargs),
        breakers={"local": CircuitBreaker()},
        key_resolver=EnvKeyResolver(),
    )
    # Through `build_artifact_model`, not by constructing `RoutedArtifactModel` directly: the
    # factory is what resolves the model identifier from the tier's PRIMARY endpoint, and that
    # resolution is the thing that stops a request naming a model the endpoint does not serve.
    model = build_artifact_model(router=router, tier_config=config, tier_name=ModelTier.SELF_HOSTED.value)
    assert model is not None
    return GenerationService(model=model, max_attempts=max_attempts)


async def _run(service: GenerationService, prompt: str = "a python checkout service"):
    outcome = GenerationOutcome(run_id=uuid.uuid4())
    frames = [
        frame async for frame in service.stream_generation(uuid.uuid4(), prompt, outcome=outcome, project=PROJECT)
    ]
    events = [frame.split("\n", 1)[0].removeprefix("event: ") for frame in frames]
    payloads = [json.loads(frame.split("\ndata: ", 1)[1].rstrip("\n")) for frame in frames]
    return outcome, events, payloads


class TestAGenuineModelCallRecordsProvider:
    async def test_served_from_is_provider_and_the_endpoint_is_named(self) -> None:
        """The headline: a run that reached an endpoint says `provider`, not `template`."""
        outcome, events, _ = await _run(_service(lambda r: httpx.Response(200, content=_sse_for(GOOD_OUTPUT))))

        assert outcome.served_from == "provider"
        assert outcome.served_from in SERVED_FROM, "the value must satisfy the DB CHECK constraint"
        assert outcome.tier == "self_hosted"
        assert outcome.endpoint_id == "local"
        assert outcome.status == "accepted"
        assert outcome.iterations_used == 1
        assert events[-1] == SSEEventType.COMPLETE.value

    async def test_the_artifacts_are_the_models_bytes_not_a_template(self) -> None:
        """A run recorded as `provider` must carry the PROVIDER's content.

        The failure this guards against is the one the task exists to remove: a stubbed or
        template-filled artifact set on a row that claims a model produced it.
        """
        outcome, _, _ = await _run(_service(lambda r: httpx.Response(200, content=_sse_for(GOOD_OUTPUT))))
        by_path = {f.path: f.content for f in outcome.files}

        assert sorted(by_path) == ["Dockerfile", "k8s/deployment.yaml", "k8s/ingress.yaml", "k8s/service.yaml"]
        # `RUN pip install ... requirements.txt` on its own line and `COPY . .` are the fixture's
        # wording; `service.py::_render`'s Dockerfile has neither in this arrangement.
        assert "RUN pip install --no-cache-dir -r requirements.txt" in by_path["Dockerfile"]
        assert "replicas: 1" in by_path["k8s/deployment.yaml"]

    async def test_the_token_frames_carry_the_providers_deltas(self) -> None:
        """§7.4's `token` event must carry model output, which is problem (3).

        Asserted by JOINING the token payloads and comparing to what the provider sent. Slicing a
        finished string would also produce token frames, so the only assertion that distinguishes
        the two is that the frames reconstruct the provider's bytes exactly.
        """
        _, events, payloads = await _run(_service(lambda r: httpx.Response(200, content=_sse_for(GOOD_OUTPUT))))

        tokens = [p["text"] for e, p in zip(events, payloads, strict=True) if e == SSEEventType.TOKEN.value]
        assert tokens, "no token frame was emitted"
        assert "".join(tokens) == GOOD_OUTPUT
        # And none of them is a replay: these arrived from the provider as it produced them.
        assert not any(
            p.get("replayed") for e, p in zip(events, payloads, strict=True) if e == SSEEventType.TOKEN.value
        )

    async def test_every_event_name_stays_in_the_vocabulary(self) -> None:
        """Q-26's clause, over the provider path this time.

        Q-26 drives `GenerationService()` with no router, so the whole model path is outside what it
        quantifies over. Restated here rather than assumed.
        """
        _, events, _ = await _run(_service(lambda r: httpx.Response(200, content=_sse_for(GOOD_OUTPUT))))
        allowed = {member.value for member in SSEEventType}
        assert set(events) <= allowed, f"names outside §7.4: {set(events) - allowed}"
        terminals = [e for e in events if e in {"complete", "error"}]
        assert len(terminals) == 1 and events[-1] == terminals[0]


class TestACacheHitRecordsL1:
    async def test_the_second_identical_run_is_served_from_l1_with_no_provider_call(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, content=_sse_for(GOOD_OUTPUT))

        redis = _Redis()
        first = await _run(_service(handler, redis=redis))
        assert first[0].served_from == "provider"
        assert calls["n"] == 1

        # A NEW service over the SAME Redis, which is how a second HTTP request arrives: the cache
        # is shared through the router the lifespan composed, not held in a service instance.
        outcome, events, payloads = await _run(_service(handler, redis=redis))

        assert outcome.served_from == "l1"
        assert calls["n"] == 1, "the repeat reached the provider, so the cache did not serve it"
        # A cache hit consumed no provider attempt, and recording one would inflate the NFR-04
        # iteration average the column exists to measure.
        assert outcome.iterations_used == 0
        assert outcome.status == "accepted"
        assert events[-1] == SSEEventType.COMPLETE.value

    async def test_the_cached_content_still_reaches_the_client_as_token_frames(self) -> None:
        """A hit must not be a silent stream: the client asked for the artifacts, not for a receipt."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse_for(GOOD_OUTPUT))

        redis = _Redis()
        await _run(_service(handler, redis=redis))
        _, events, payloads = await _run(_service(handler, redis=redis))

        tokens = [p for e, p in zip(events, payloads, strict=True) if e == SSEEventType.TOKEN.value]
        assert tokens
        assert "".join(t["text"] for t in tokens) == GOOD_OUTPUT
        # Labelled as a replay, so a client can distinguish it from live production.
        assert all(t.get("replayed") is True for t in tokens)


class TestANearDuplicateRecordsL2:
    async def test_a_near_duplicate_prompt_is_served_from_l2(self) -> None:
        """`served_from='l2'`, end to end from the generation surface.

        The embedder is a deterministic character-frequency vector, so "near" is a property of the
        text rather than of a model's mood — the same reasoning `test_semantic_cache.py` states for
        its bag-of-words fixture. The threshold stays at the configured 0.95; the pair is one this
        embedder genuinely rates as near, rather than the threshold being lowered until it passed.
        """

        async def embed(text: str) -> list[float]:
            counts = [0.0] * 27
            for character in text.lower():
                if character.isalpha() and character.isascii():
                    counts[ord(character) - ord("a")] += 1.0
                elif character.isspace():
                    counts[26] += 1.0
            return counts

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse_for(GOOD_OUTPUT))

        redis = _Redis()
        first, _, _ = await _run(_service(handler, redis=redis, embed=embed), prompt="a python checkout service")
        assert first.served_from == "provider"

        # Differs in surface form only — trailing punctuation and case. L1 must miss, because the
        # key is a digest of the prompt; only similarity can serve it.
        outcome, events, _ = await _run(
            _service(handler, redis=redis, embed=embed), prompt="A python checkout service."
        )

        assert outcome.served_from == "l2", (
            "the near-duplicate was not served from L2, so the semantic tier is not reachable from "
            "the generation surface"
        )
        assert outcome.iterations_used == 0
        assert events[-1] == SSEEventType.COMPLETE.value


class TestTheTemplateIsReachedOnlyAfterTheProviderFails:
    async def test_three_transport_failures_then_template(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, text="the model server is down")

        outcome, events, payloads = await _run(_service(handler))

        # §3.8's bound: three attempts, not four and not one.
        assert calls["n"] == 3
        assert outcome.served_from == "template"
        assert outcome.tier == "template"
        # `template_fallback`, NOT `accepted`. The two are different facts about a run, and
        # collapsing them would hide every provider outage behind a green row.
        assert outcome.status == "template_fallback"
        assert events[-1] == SSEEventType.COMPLETE.value
        assert payloads[-1]["served_from"] == "template"
        assert payloads[-1]["provider_findings"], "a fallback must say why it fell back"

    async def test_unparseable_output_is_retried_and_then_falls_back(self) -> None:
        """Output the contract cannot read is a failed attempt, not a synthesised artifact set."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, content=_sse_for("Sure! Here is a Dockerfile for you.\n"))

        outcome, _, payloads = await _run(_service(handler))
        assert calls["n"] == 3
        assert outcome.served_from == "template"
        assert outcome.status == "template_fallback"
        assert any("Dockerfile" in finding for finding in payloads[-1]["provider_findings"])

    async def test_output_that_fails_the_gate_is_retried_and_then_falls_back(self) -> None:
        """A root-running Dockerfile is refused, and the refusal reaches the retry prompt."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse_for(ROOTFUL_OUTPUT))

        outcome, events, payloads = await _run(_service(handler))
        assert outcome.served_from == "template"
        assert outcome.status == "template_fallback"
        validations = [p for e, p in zip(events, payloads, strict=True) if e == SSEEventType.VALIDATION.value]
        assert any("USER" in finding for v in validations for finding in v["findings"])

    async def test_the_retry_prompt_differs_so_the_cache_cannot_serve_the_rejection(self) -> None:
        """The subtle one, and the reason findings are quoted back into the prompt.

        The cache key is computed over the prompt. An identical retry would be served from L1 with
        the same rejected content, and the bounded loop would spend all three attempts on one bad
        answer without ever reaching the provider again.
        """
        bodies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content)["messages"][0]["content"])
            return httpx.Response(200, content=_sse_for(ROOTFUL_OUTPUT))

        await _run(_service(handler))
        assert len(bodies) == 3, f"the provider was called {len(bodies)} times, not three"
        assert len(set(bodies)) == 3, "two attempts sent the identical prompt, so L1 would serve the rejection"
        assert "USER" in bodies[1], "the retry prompt does not quote the gate's finding"

    async def test_a_service_with_no_router_is_the_template_path_and_says_so(self) -> None:
        """A deployment with no reachable endpoint is a configuration, not an error.

        `accepted` rather than `template_fallback` here: nothing was tried and nothing failed. The
        distinction is what lets an operator tell a deliberate template deployment from a degraded
        one.
        """
        outcome, events, _ = await _run(GenerationService())
        assert outcome.served_from == "template"
        assert outcome.tier == "template"
        assert outcome.status == "accepted"
        assert outcome.iterations_used == 0
        assert events[-1] == SSEEventType.COMPLETE.value

    async def test_attempted_tier_reports_what_the_running_row_should_record(self) -> None:
        service = _service(lambda r: httpx.Response(200, content=_sse_for(GOOD_OUTPUT)))
        assert service.attempted_tier == "self_hosted"
        assert GenerationService().attempted_tier == "template"


class TestTheIterationBoundIsRefusedAtConstruction:
    """§3.8's bound is expressed in the type, the schema and Q-08. This is the fourth place."""

    @pytest.mark.parametrize("bad", [0, -1, 4, 10])
    async def test_an_out_of_range_attempt_count_is_refused(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            GenerationService(max_attempts=bad)


class TestThePortsVocabularyIsStorable:
    """The seam's declared output must be a subset of what the CHECK constraint admits.

    `core/model_port.SERVED_FROM` and `generation/models.SERVED_FROM` are in different domains and
    cannot import each other, so they are two lists of strings that have to agree. A value the port
    can emit and the database refuses would fail at the very END of an otherwise successful run,
    which is the worst place to discover it. A test is where they meet.
    """

    def test_every_port_origin_is_in_the_column_vocabulary(self) -> None:
        assert set(PORT_SERVED_FROM) <= set(SERVED_FROM), (
            f"the model port can report {sorted(set(PORT_SERVED_FROM) - set(SERVED_FROM))}, which "
            f"ck_generation_runs_served_from_allowed would refuse"
        )

    def test_the_column_vocabulary_adds_only_the_non_model_origins(self) -> None:
        """Stated in the other direction, so a value added to the column is a deliberate act.

        `template` means no model was involved and `pending` means no outcome yet; `l3` is a
        designed cache tier with no writer (§13.4). None of the three can come from a model port,
        and if one ever could, this test is where that decision has to be recorded.
        """
        assert set(SERVED_FROM) - set(PORT_SERVED_FROM) == {"template", "pending", "l3"}
