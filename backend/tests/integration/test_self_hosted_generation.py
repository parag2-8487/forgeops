# SPDX-License-Identifier: FSL-1.1-ALv2
"""A GENUINE model call through the six-tier router, against the local `ollama` service.

WHY THIS FILE IS THE ONE THAT MATTERS
-------------------------------------
Everything else about routing in this repository is provable over `httpx.MockTransport`, and most
of it is: `tests/unit/test_streaming_router.py` drives the real adapter, router, breaker and cache
over a scripted socket. What a scripted socket cannot prove is that a real model, on the other end
of the real protocol, produces something this pipeline can use — and that was the open question.
`config/model-tiers.yaml`'s hosted endpoints all need a key, `.env.example` ships placeholders
(credentials.md), and there was no self-hosted server. So `ModelRouter.complete` returned EXHAUSTED
for every tier, generation could only render templates, and `generation_runs.served_from` was a SQL
string literal.

`docker-compose.yml`'s `ollama` service closes that. These tests talk to it.

HOW ABSENCE IS HANDLED, AND WHY IT IS NOT A HIDDEN SKIP
-------------------------------------------------------
`require_capability("self_hosted_model")` — the same mechanism §0.4.4 gives every other
infrastructure dependency. It SKIPS on a developer machine that has not started the service and
FAILS when `FORGEOPS_REQUIRE_INTEGRATION=1`, so an environment that promised the capability cannot
silently drop the test. A `@pytest.mark.skip` would have been a permanent, invisible exemption;
this is a probe with a stated reason and a CI mode that refuses it.

The probe checks that the CONFIGURED MODEL IS PRESENT, not merely that the port answers. A server
without the model returns 404 `model "..." not found` for every request, and a skip reason that
said "no server" for that case would send the reader looking in the wrong place.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest
from src.ai.generation_port import build_artifact_model
from src.ai.routing.breaker import CircuitBreaker
from src.ai.routing.cache import TieredSemanticCache
from src.ai.routing.endpoints import CompletionRequest, EndpointRegistry
from src.ai.routing.keys import EnvKeyResolver
from src.ai.routing.router import ModelRouter, RoutingOutcome
from src.ai.routing.tiers import ModelTier, TierConfig, load_tier_config
from src.core.sse import SSEEventType
from src.generation.model_prompt import parse_artifacts
from src.generation.service import GenerationOutcome, GenerationService
from src.secrets.redaction import create_redacted_prompt

from .capability import require_capability
from .wiring import wires

pytestmark = pytest.mark.asyncio

#: Where a HOST process reaches the `ollama` container: its published loopback port.
#:
#: `.env.example` names the Compose service (`http://ollama:11434/v1`), which is correct inside the
#: network and unresolvable outside it — the same two-address problem `OIDC_PUBLIC_BASE_URL` exists
#: for. Overridable so a developer running Ollama natively on another port is not blocked.
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
BASE_URL_ENV = "FORGEOPS_TEST_SELF_HOSTED_BASE_URL"
MODEL_ENV = "SELF_HOSTED_MODEL_ID"
EMBEDDING_MODEL_ENV = "SELF_HOSTED_EMBEDDING_MODEL_ID"
#: Committed defaults, so this file works with no environment set at all. They are the values
#: `.env.example` ships, restated here because `scripts/local-env.ps1` deliberately clears every
#: declared project key before a host-side run.
DEFAULT_MODEL = "qwen2.5-coder:1.5b"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text:latest"

#: CPU inference is minutes, not seconds. `OUTBOUND_HTTP_TIMEOUT_SECONDS` is 60, which is a
#: PER-READ budget on the streaming path — deltas arrive far more often than that once generation
#: starts — but this file also makes non-streaming calls, where 60 s would time out on the whole
#: generation. Stated here rather than inherited so a slow machine fails on an assertion instead of
#: on a timeout that says nothing about routing.
TIMEOUT_SECONDS = 900.0


def _base_url() -> str:
    return os.environ.get(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL


def _model() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _embedding_model() -> str:
    return os.environ.get(EMBEDDING_MODEL_ENV, "").strip() or DEFAULT_EMBEDDING_MODEL


def _require_model(model: str) -> str:
    """Skip locally / fail in CI unless the server is up AND serving `model`."""
    base_url = _base_url()
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/models", timeout=5.0)
        response.raise_for_status()
        served = {str(entry.get("id")) for entry in response.json().get("data", [])}
    except Exception as exc:
        require_capability(
            "self_hosted_model",
            f"no OpenAI-compatible model server answered at {base_url} ({type(exc).__name__}); "
            f"start it with `docker compose up -d ollama`",
        )
        # `require_capability` always skips or fails, so this is unreachable. It is present because
        # the function is not typed `NoReturn` and the type checker would otherwise see `served`
        # possibly unbound below.
        raise AssertionError("require_capability returned") from exc
    if model not in served:
        require_capability(
            "self_hosted_model",
            f"the server at {base_url} does not serve {model!r} (it serves {sorted(served)}); "
            f"a request naming an absent model answers 404, not a completion",
        )
    return base_url


class _Redis:
    """The `AsyncRedisWithHashes` surface in a dict, so L1 and L2 are the real code paths.

    A real Redis is available to this suite, and `test_semantic_cache.py` uses it. It is not used
    here on purpose: these tests are about whether a REAL MODEL's output can be cached and matched,
    and a second live dependency would add a way for them to fail that says nothing about that.
    """

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


def _router(base_url: str, model: str, *, redis: _Redis, embed=None) -> ModelRouter:
    """The real router over the COMMITTED tier YAML, with only the two env values supplied.

    `load_tier_config` on the shipped file rather than a hand-built `TierConfig`, because the
    question this file answers includes "is the committed configuration the thing that reaches a
    model" — debt D1's question, one layer up from where Q-27 asks it.
    """
    config = _tier_config(base_url, model)
    cache_kwargs: dict[str, object] = {"redis": redis}
    if embed is not None:
        cache_kwargs |= {"embed": embed, "similarity_threshold": 0.95}
    return ModelRouter(
        tier_config=config,
        registry=EndpointRegistry.from_config(config, http=httpx.AsyncClient(timeout=TIMEOUT_SECONDS)),
        cache=TieredSemanticCache(**cache_kwargs),  # type: ignore[arg-type]
        breakers={eid: CircuitBreaker() for eid in config.endpoints},
        key_resolver=EnvKeyResolver(),
    )


def _tier_config(base_url: str, model: str) -> TierConfig:
    """Parse the committed YAML with the two self-hosted variables supplied."""
    tier_yaml = Path(__file__).resolve().parents[2] / "config" / "model-tiers.yaml"
    env = dict(os.environ)
    env["SELF_HOSTED_BASE_URL"] = base_url
    env["SELF_HOSTED_MODEL_ID"] = model
    # The hosted endpoints' base URLs must expand or the load fails; they are never called here.
    for name, value in (
        ("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        ("XAI_BASE_URL", "https://api.x.ai/v1"),
        ("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        ("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        ("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com"),
    ):
        env.setdefault(name, value)
    return load_tier_config(tier_yaml, env=env)


def _generation_service(base_url: str, model: str, *, redis: _Redis, embed=None) -> GenerationService:
    """A service wired to the self-hosted tier through the production factory.

    `build_artifact_model` rather than a hand-built adapter, because the factory is what resolves
    the model identifier from the tier's primary endpoint — the resolution that stops a request
    naming a model the endpoint does not serve.
    """
    port = build_artifact_model(
        router=_router(base_url, model, redis=redis, embed=embed),
        tier_config=_tier_config(base_url, model),
        tier_name=ModelTier.SELF_HOSTED.value,
        max_tokens=2048,
    )
    assert port is not None, "build_artifact_model refused the committed self-hosted tier"
    return GenerationService(model=port)


class TestTheCommittedConfigurationReachesARealModel:
    async def test_the_self_hosted_tier_answers_a_completion(self) -> None:
        """`ModelRouter.complete` returning OK from a REAL endpoint, which it never had.

        The assertion is `served_from == "endpoint"` with a named endpoint id, over the committed
        YAML. Before the `ollama` service existed this could only be EXHAUSTED.
        """
        model = _model()
        base_url = _require_model(model)
        router = _router(base_url, model, redis=_Redis())

        result = await router.complete(
            tier=ModelTier.SELF_HOSTED,
            request=CompletionRequest(
                model=model,
                messages=[{"role": "user", "content": "Reply with the single word: OK"}],
                temperature=0.0,
                max_tokens=16,
            ),
            prompt=create_redacted_prompt("Reply with the single word: OK"),
        )

        assert result.outcome is RoutingOutcome.OK, [(a.endpoint_id, a.result, a.reason) for a in result.attempts]
        assert result.served_from == "endpoint"
        assert result.endpoint_id == "qwen3-coder-next"
        assert result.content and result.content.strip(), "the model returned no content"
        assert result.streamed is False

    async def test_the_model_id_comes_from_the_expanded_tier_yaml(self) -> None:
        """The `${SELF_HOSTED_MODEL_ID}` expansion, asserted where it matters.

        The tier file used to carry the literal `qwen3-coder-next` as the model, which no real
        server has, so the ONE reachable endpoint answered 404 for every request.
        """
        model = _model()
        _require_model(model)
        from pathlib import Path

        env = dict(os.environ)
        env["SELF_HOSTED_BASE_URL"] = _base_url()
        env["SELF_HOSTED_MODEL_ID"] = model
        for name, value in (
            ("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            ("XAI_BASE_URL", "https://api.x.ai/v1"),
            ("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            ("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            ("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com"),
        ):
            env.setdefault(name, value)
        config = load_tier_config(Path(__file__).resolve().parents[2] / "config" / "model-tiers.yaml", env=env)
        assert config.endpoints["qwen3-coder-next"].model == model

    async def test_real_deltas_arrive_as_the_model_produces_them(self) -> None:
        """§7.4's `token` event carrying ACTUAL model output, from an actual model.

        More than one delta is the assertion that distinguishes streaming from slicing: a
        whole-response call joined and handed over in one piece would deliver exactly one.
        """
        model = _model()
        base_url = _require_model(model)
        router = _router(base_url, model, redis=_Redis())
        deltas: list[str] = []

        async def sink(text: str) -> None:
            deltas.append(text)

        result = await router.complete(
            tier=ModelTier.SELF_HOSTED,
            request=CompletionRequest(
                model=model,
                messages=[{"role": "user", "content": "Count from one to five, one number per line."}],
                temperature=0.0,
                max_tokens=64,
            ),
            prompt=create_redacted_prompt("Count from one to five, one number per line."),
            on_token=sink,
        )

        assert result.outcome is RoutingOutcome.OK
        assert result.streamed is True
        assert len(deltas) > 1, f"only {len(deltas)} delta(s) arrived, so nothing was streamed"
        assert "".join(deltas) == result.content
        assert result.usage is not None and result.usage["completion_tokens"] > 0


class TestGenerationProducesArtifactsFromARealModel:
    # `app.state.artifact_model` is composed in `create_app`, and §0.4.1 requires every composed
    # collaborator to be driven through the REAL object graph by a declared wiring test —
    # `test_wiring_coverage.py` asserts `composed ⊆ declared` and named this one as undeclared.
    #
    # This test qualifies rather than merely claiming to: `_generation_service` builds the port with
    # the production `build_artifact_model` factory over the committed tier config, and the assertions
    # below run a real model through it. A declaration on a test that used a hand-built adapter would
    # satisfy the checker while proving nothing about the composition.
    @wires("artifact_model")
    async def test_a_run_is_served_from_provider_and_the_artifacts_pass_the_gate(self) -> None:
        """The end-to-end claim: a real model's output satisfies §11.5.5's deterministic gate.

        This is the assertion behind `served_from='provider'`. It is a real model producing real
        artifacts that a real gate accepts — no fixture stands in for any of the three.
        """
        model = _model()
        base_url = _require_model(model)
        service = _generation_service(base_url, model, redis=_Redis())
        outcome = GenerationOutcome(run_id=uuid.uuid4())
        events = [
            frame.split("\n", 1)[0].removeprefix("event: ")
            async for frame in service.stream_generation(
                uuid.uuid4(),
                "a python checkout service",
                outcome=outcome,
                project={"name": "checkout-api", "settings": {}},
            )
        ]

        assert outcome.served_from == "provider", (
            f"the run was served from {outcome.served_from!r}; the deterministic gate's verdict was "
            f"{outcome.validation_passed} on {[f.path for f in outcome.files]}"
        )
        assert outcome.status == "accepted"
        assert outcome.tier == "self_hosted"
        assert outcome.endpoint_id == "qwen3-coder-next"
        assert 1 <= outcome.iterations_used <= 3
        assert outcome.completion_tokens > 0
        assert [f.path for f in outcome.files] == [
            "Dockerfile",
            "k8s/deployment.yaml",
            "k8s/service.yaml",
            "k8s/ingress.yaml",
        ]
        # The gate's own clauses, restated over the model's bytes so a green run cannot be a
        # template that slipped through.
        dockerfile = next(f.content for f in outcome.files if f.path == "Dockerfile")
        assert dockerfile.startswith("FROM ")
        assert "USER " in dockerfile
        assert events[-1] == SSEEventType.COMPLETE.value
        assert set(events) <= {member.value for member in SSEEventType}

    async def test_the_second_identical_run_is_served_from_the_cache(self) -> None:
        """L1 over a REAL model's response, so the cached bytes are a model's and not a fixture's."""
        model = _model()
        base_url = _require_model(model)
        redis = _Redis()

        first = GenerationOutcome(run_id=uuid.uuid4())
        service = _generation_service(base_url, model, redis=redis)
        async for _ in service.stream_generation(
            uuid.uuid4(), "a python checkout service", outcome=first, project={"name": "checkout-api", "settings": {}}
        ):
            pass
        if first.served_from != "provider":
            pytest.fail(
                f"the first run was served from {first.served_from!r}, so the cache assertion below would prove nothing"
            )

        second = GenerationOutcome(run_id=uuid.uuid4())
        # A NEW service and a NEW router over the SAME Redis: the cache is shared through the
        # composed router, not held by a service instance, which is how two HTTP requests see it.
        repeat = _generation_service(base_url, model, redis=redis)
        async for _ in repeat.stream_generation(
            uuid.uuid4(), "a python checkout service", outcome=second, project={"name": "checkout-api", "settings": {}}
        ):
            pass

        assert second.served_from == "l1"
        assert second.iterations_used == 0
        # Same artifacts, from the cache rather than from the model.
        assert [f.content for f in second.files] == [f.content for f in first.files]


class TestTheSelfHostedEmbedderMakesL2LiveOverARealModel:
    async def test_a_near_duplicate_generation_prompt_is_served_from_l2(self) -> None:
        """Criterion 14's L2 clause with a REAL embedding model behind it.

        `test_semantic_cache.py` proves the threshold arithmetic with a deterministic bag-of-words
        fixture and says why: a learned model would make the assertion depend on its weights. That
        argument is right for a property test and it leaves one thing unproven — that a real
        embedding provider, reached over the real transport, rates a near-duplicate above 0.95 at
        all. This asserts that, and it is why the pair below differs only in surface form.
        """
        model = _model()
        embedding_model = _embedding_model()
        base_url = _require_model(model)
        _require_model(embedding_model)

        from src.ai.embeddings import SelfHostedEmbedder

        embedder = SelfHostedEmbedder(base_url=base_url, model=embedding_model, timeout_seconds=TIMEOUT_SECONDS)
        redis = _Redis()

        first = GenerationOutcome(run_id=uuid.uuid4())
        service = _generation_service(base_url, model, redis=redis, embed=embedder)
        async for _ in service.stream_generation(
            uuid.uuid4(), "a python checkout service", outcome=first, project={"name": "checkout-api", "settings": {}}
        ):
            pass
        if first.served_from != "provider":
            pytest.fail(f"the first run was served from {first.served_from!r}; nothing was cached to match")

        second = GenerationOutcome(run_id=uuid.uuid4())
        repeat = _generation_service(base_url, model, redis=redis, embed=embedder)
        async for _ in repeat.stream_generation(
            uuid.uuid4(),
            # Surface form only: capitalisation and a full stop. The two prompts hash differently,
            # so L1 must miss and only similarity can serve it.
            "A python checkout service.",
            outcome=second,
            project={"name": "checkout-api", "settings": {}},
        ):
            pass

        assert second.served_from == "l2", (
            f"the near-duplicate was served from {second.served_from!r}; L2 over a real embedding "
            f"model did not rate it above the 0.95 threshold"
        )
        assert second.iterations_used == 0

    async def test_the_embedder_is_input_sensitive_which_is_what_makes_l2_safe(self) -> None:
        """Two unrelated texts must not embed to the same vector.

        `EmbeddingOrchestrator`'s fallback returns `[0.01 * (i % 100) for i in range(1024)]`
        regardless of input, and L2 over that would make every prompt a near-duplicate of every
        other — the cache would serve an arbitrary completion for any question. So this is the
        precondition for enabling L2 at all, asserted against the live provider rather than assumed
        from its name.
        """
        embedding_model = _embedding_model()
        base_url = _require_model(embedding_model)

        from src.ai.embeddings import SelfHostedEmbedder
        from src.ai.routing.cache import cosine_similarity

        embedder = SelfHostedEmbedder(base_url=base_url, model=embedding_model, timeout_seconds=TIMEOUT_SECONDS)
        one = await embedder("a python checkout service on kubernetes")
        two = await embedder("the history of eighteenth century naval cartography")

        assert one and two
        assert len(one) == len(two)
        assert one != two, "the embedder ignored its input, so it cannot be used as a similarity key"
        assert cosine_similarity(one, two) < 0.95, (
            "two unrelated texts scored above the admission threshold, so L2 would serve one for the other"
        )


class TestTheParseContractHoldsOnRealModelOutput:
    async def test_a_real_response_parses_into_the_four_required_artifacts(self) -> None:
        """The parse, exercised on bytes a model actually emitted rather than on a fixture.

        `tests/unit/test_generation_model_prompt.py` covers the parser's edge cases with fixtures,
        which is where fixtures belong. What it cannot cover is whether a 1.5B-class model's real
        output shape is one this parser reads — the question the strict format was chosen to answer.
        """
        model = _model()
        base_url = _require_model(model)
        service = _generation_service(base_url, model, redis=_Redis())
        outcome = GenerationOutcome(run_id=uuid.uuid4())
        raw: list[str] = []
        async for frame in service.stream_generation(
            uuid.uuid4(),
            "a python checkout service",
            outcome=outcome,
            project={"name": "checkout-api", "settings": {}},
        ):
            if frame.startswith(f"event: {SSEEventType.TOKEN.value}\n"):
                import json

                raw.append(json.loads(frame.split("\ndata: ", 1)[1].rstrip("\n"))["text"])

        assert outcome.served_from == "provider", f"served from {outcome.served_from!r}, so there is no model output"
        # The token frames reconstruct the model's response, and that response parses. Both halves
        # matter: the first is problem (3), the second is what makes the artifacts usable.
        parsed = parse_artifacts("".join(raw))
        assert sorted(parsed) == ["Dockerfile", "k8s/deployment.yaml", "k8s/ingress.yaml", "k8s/service.yaml"]
