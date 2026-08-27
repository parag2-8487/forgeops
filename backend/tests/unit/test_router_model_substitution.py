"""Each endpoint in a cascade is asked for ITS OWN model.

WHY THIS IS A SEPARATE FILE. `test_streaming_router.py`'s harness gives every endpoint the same
`test-model`, which is the right simplification for asserting streaming behaviour and precisely the
wrong one here: a test where all endpoints share a model cannot tell whether the router substitutes
per endpoint or forwards one name to all of them. That is the distinction this file exists for.

FOUND BY TRYING TO PROVE FAILOVER, not by reading the code. Driving the real `self_hosted` chain the
way `ai/routes.py` drives it — `CompletionRequest(model=body.tier, ...)`, so the literal string
`"self_hosted"` — returned `404 Not Found` from BOTH live model servers, because neither has a model
by that name. `_payload` sends `request.model` verbatim, and nothing on the invocation path had ever
read the `model:` that `config/model-tiers.yaml` declares for each endpoint.

Across vendors the consequence is total: no OpenAI deployment serves `claude-fable-5`, so a cascade
that fell through from the primary would have asked the fallback for the primary's model and been
refused. That is a failover bug that only appears when failover actually happens, which is why it
survived a suite that never made one endpoint fail over to a differently-modelled one.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from src.ai.routing.breaker import CircuitBreaker
from src.ai.routing.cache import TieredSemanticCache
from src.ai.routing.endpoints import CompletionRequest, EndpointRegistry
from src.ai.routing.keys import EnvKeyResolver
from src.ai.routing.router import ModelRouter, RoutingOutcome
from src.ai.routing.tiers import (
    EndpointDescriptor,
    EndpointProtocol,
    ModelTier,
    TierChain,
    TierConfig,
)
from src.secrets.redaction import create_redacted_prompt

pytestmark = pytest.mark.asyncio


class _Redis:
    """The two-method surface `TieredSemanticCache` needs, so the cache is real and the server is not."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> bytes | None:
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    async def set(self, key: str, value: str | bytes, *, ex: int | None = None) -> bool:
        self.store[key] = value.decode("utf-8") if isinstance(value, bytes) else value
        return True


def _completion(content: str = "ok") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _router(handler, *, models: dict[str, str]) -> ModelRouter:
    """A cascade whose endpoints each declare a DIFFERENT model."""
    ids = tuple(models)
    endpoints = {
        eid: EndpointDescriptor(
            id=eid,
            provider="test",
            model=model,
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url=f"http://{eid}.invalid/v1",
            key_ref=None,
        )
        for eid, model in models.items()
    }
    config = TierConfig(
        tiers={
            ModelTier.SELF_HOSTED: TierChain(
                primary=ids[0],
                secondary=ids[1] if len(ids) > 1 else None,
            )
        },
        endpoints=endpoints,
    )
    return ModelRouter(
        tier_config=config,
        registry=EndpointRegistry.from_config(config, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
        cache=TieredSemanticCache(redis=_Redis()),
        breakers={eid: CircuitBreaker() for eid in ids},
        key_resolver=EnvKeyResolver(),
    )


async def _complete(router: ModelRouter, *, asked_for: str) -> Any:
    text = "hello"
    return await router.complete(
        tier=ModelTier.SELF_HOSTED,
        request=CompletionRequest(model=asked_for, messages=[{"role": "user", "content": text}]),
        prompt=create_redacted_prompt(text),
    )


class TestEachEndpointIsAskedForItsOwnModel:
    async def test_the_primary_receives_the_model_it_declares_not_the_one_requested(self) -> None:
        """The caller names a tier, not a model — `ai/routes.py` passes `body.tier`."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["model"])
            return httpx.Response(200, json=_completion())

        result = await _complete(
            _router(handler, models={"primary": "vendor-a-large"}),
            asked_for="self_hosted",
        )

        assert result.outcome is RoutingOutcome.OK
        # NOT "self_hosted", which is what the caller said and what no provider serves.
        assert seen == ["vendor-a-large"]

    async def test_the_fallback_receives_its_own_model_not_the_primary_s(self) -> None:
        """The bug in one assertion: a cascade must not ask vendor B for vendor A's model."""
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            seen.append((host, json.loads(request.content)["model"]))
            if host == "primary.invalid":
                # The primary is down, so the cascade has to fall through.
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(200, json=_completion())

        result = await _complete(
            _router(handler, models={"primary": "vendor-a-large", "secondary": "vendor-b-large"}),
            asked_for="self_hosted",
        )

        assert result.outcome is RoutingOutcome.OK
        assert result.endpoint_id == "secondary"
        assert seen == [
            ("primary.invalid", "vendor-a-large"),
            ("secondary.invalid", "vendor-b-large"),
        ]

    async def test_an_endpoint_with_no_declared_model_keeps_what_the_caller_asked_for(self) -> None:
        """A descriptor carrying no model must not silently blank the request's.

        `EndpointDescriptor.model` is a plain string, so an empty one is possible; substituting it
        would send `"model": ""` and turn a configuration omission into a provider error that names
        the wrong thing.
        """
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["model"])
            return httpx.Response(200, json=_completion())

        result = await _complete(_router(handler, models={"primary": ""}), asked_for="caller-model")

        assert result.outcome is RoutingOutcome.OK
        assert seen == ["caller-model"]

    async def test_the_cache_key_is_the_caller_s_model_so_a_fallback_cannot_poison_it(self) -> None:
        """One prompt through two different endpoints must not produce two cache identities.

        The cache is keyed on `request.model` — the CALLER's — which is what makes a fallback's answer
        reusable for the same request later. Keying on the endpoint's model instead would give the
        same prompt a different key depending on which endpoint happened to answer, so a cache hit
        would depend on which server was up at the time.
        """
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            calls.append(host)
            if host == "primary.invalid":
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(200, json=_completion("cached body"))

        router = _router(handler, models={"primary": "vendor-a-large", "secondary": "vendor-b-large"})

        first = await _complete(router, asked_for="self_hosted")
        assert first.endpoint_id == "secondary"

        # The same request again. It must come from the cache, not from either endpoint.
        before = len(calls)
        second = await _complete(router, asked_for="self_hosted")
        assert second.outcome is RoutingOutcome.OK
        assert second.content == "cached body"
        assert len(calls) == before, "the second identical request reached an endpoint"
