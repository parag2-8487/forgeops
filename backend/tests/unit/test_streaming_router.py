# SPDX-License-Identifier: FSL-1.1-ALv2
"""Streaming completions through the cascade, over a fake transport (design §11.7, §7.4).

WHAT THESE COVER THAT NOTHING ELSE DOES
---------------------------------------
`OpenAICompatibleEndpoint` could only ask for a whole response, so §7.4's `token` event had no
model output to carry and `GenerationService._chunks` sliced a finished string every 120
characters — "Not real tokenisation, and named so", in its own docstring.

The transport here is `httpx.MockTransport`, which §0.4.1 permits: it substitutes the WIRE, not a
collaborator. The endpoint adapter, the router, the breaker, the cache and the dedup are all the
real objects, so what is asserted is the real cascade's behaviour over a scripted socket. The
HOSTED-key path is covered here rather than against a live provider because `.env.example` ships
placeholder keys deliberately; the LOCAL path is exercised against a real model in
`tests/integration/test_self_hosted_generation.py`.
"""

from __future__ import annotations

import json

import httpx
import pytest
from src.ai.routing.breaker import CircuitBreaker
from src.ai.routing.cache import TieredSemanticCache
from src.ai.routing.endpoints import (
    CompletionRequest,
    EndpointRegistry,
    MalformedResponseError,
    OpenAICompatibleEndpoint,
    StreamingModelEndpoint,
)
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

from tests.synthetic_secrets import SYNTHETIC_MARKER, bearer_with

pytestmark = pytest.mark.asyncio


def _sse(*frames: dict[str, object]) -> bytes:
    """Encode provider frames as the OpenAI streaming wire format, terminator included."""
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    return (body + "data: [DONE]\n\n").encode("utf-8")


def _delta(text: str) -> dict[str, object]:
    return {"model": "test-model", "choices": [{"index": 0, "delta": {"content": text}}]}


def _descriptor(endpoint_id: str, *, base_url: str = "http://provider.invalid/v1", key_ref: str | None = None):
    return EndpointDescriptor(
        id=endpoint_id,
        provider="test",
        model="test-model",
        protocol=EndpointProtocol.OPENAI_COMPATIBLE,
        base_url=base_url,
        key_ref=key_ref,
    )


def _request() -> CompletionRequest:
    return CompletionRequest(model="test-model", messages=[{"role": "user", "content": "hello"}])


class _Redis:
    """The two-method `AsyncRedisLike` surface, so the cache is real and the server is not."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> bytes | None:
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    async def set(self, key: str, value: str | bytes, *, ex: int | None = None) -> bool:
        self.store[key] = value.decode("utf-8") if isinstance(value, bytes) else value
        return True


def _router(handler, *, endpoint_ids: tuple[str, ...] = ("primary",), redis: _Redis | None = None) -> ModelRouter:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    endpoints = {eid: _descriptor(eid, base_url=f"http://{eid}.invalid/v1") for eid in endpoint_ids}
    config = TierConfig(
        tiers={
            ModelTier.SELF_HOSTED: TierChain(
                primary=endpoint_ids[0],
                secondary=endpoint_ids[1] if len(endpoint_ids) > 1 else None,
            )
        },
        endpoints=endpoints,
    )
    return ModelRouter(
        tier_config=config,
        registry=EndpointRegistry.from_config(config, http=http),
        cache=TieredSemanticCache(redis=redis or _Redis()),
        breakers={eid: CircuitBreaker() for eid in endpoint_ids},
        key_resolver=EnvKeyResolver(),
    )


class TestTheAdapterSpeaksTheStreamingProtocol:
    async def test_it_asks_the_provider_to_stream(self) -> None:
        """Without `stream: true` the body arrives in one piece and the deltas are our invention."""
        asked: list[bool] = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(json.loads(request.content).get("stream") is True)
            return httpx.Response(200, content=_sse(_delta("FROM "), _delta("python")))

        endpoint = OpenAICompatibleEndpoint(
            descriptor=_descriptor("primary"),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        async def sink(text: str) -> None:
            return None

        await endpoint.complete_streaming(_request(), on_token=sink)
        assert asked == [True]

    async def test_deltas_arrive_in_order_and_join_to_the_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse(_delta("FROM "), _delta("python"), _delta(":3.11")))

        endpoint = OpenAICompatibleEndpoint(
            descriptor=_descriptor("primary"),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        seen: list[str] = []

        async def sink(text: str) -> None:
            seen.append(text)

        response = await endpoint.complete_streaming(_request(), on_token=sink)

        assert seen == ["FROM ", "python", ":3.11"]
        # The assembled content is exactly the deltas joined, which is what makes the cached value
        # and the streamed value the same bytes.
        assert response.content == "FROM python:3.11"

    async def test_it_satisfies_the_streaming_protocol(self) -> None:
        endpoint = OpenAICompatibleEndpoint(descriptor=_descriptor("primary"))
        # `ModelRouter` decides whether to stream with this exact check, so a refactor that renamed
        # the method would silently drop every deployment back to whole-response mode.
        assert isinstance(endpoint, StreamingModelEndpoint)

    async def test_a_stream_with_no_content_is_malformed_rather_than_an_empty_success(self) -> None:
        """The §11.7.1a argument, applied to the streaming path.

        An empty-string success from the primary would be SERVED, and the cascade would never reach
        the next endpoint — the whole point of `malformed_response` being a distinct attempt result.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse({"choices": [{"index": 0, "delta": {}}]}))

        endpoint = OpenAICompatibleEndpoint(
            descriptor=_descriptor("primary"),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        async def sink(text: str) -> None:  # pragma: no cover - must never be called
            raise AssertionError("a contentless stream delivered a token")

        with pytest.raises(MalformedResponseError):
            await endpoint.complete_streaming(_request(), on_token=sink)

    async def test_a_non_json_data_line_is_skipped_rather_than_fatal(self) -> None:
        """Keep-alives and the `[DONE]` sentinel are protocol, not corruption."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = f": keep-alive\n\ndata: not json at all\n\ndata: {json.dumps(_delta('ok'))}\n\ndata: [DONE]\n\n"
            return httpx.Response(200, content=body.encode("utf-8"))

        endpoint = OpenAICompatibleEndpoint(
            descriptor=_descriptor("primary"),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        seen: list[str] = []

        async def sink(text: str) -> None:
            seen.append(text)

        response = await endpoint.complete_streaming(_request(), on_token=sink)
        assert seen == ["ok"]
        assert response.content == "ok"

    async def test_the_completion_count_falls_back_to_the_delta_count(self) -> None:
        """Most providers omit `usage` from streamed frames, and `generation_runs` needs a number.

        Counting deltas is a measurement of what this process received. Reporting zero would make
        NFR-04's cost evidence read as free.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse(_delta("a"), _delta("b"), _delta("c")))

        endpoint = OpenAICompatibleEndpoint(
            descriptor=_descriptor("primary"),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        async def sink(text: str) -> None:
            return None

        response = await endpoint.complete_streaming(_request(), on_token=sink)
        assert response.usage["completion_tokens"] == 3

    async def test_a_reported_usage_wins_over_the_delta_count(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=_sse(
                    _delta("a"),
                    {"choices": [{"index": 0, "delta": {}}], "usage": {"completion_tokens": 42, "prompt_tokens": 7}},
                ),
            )

        endpoint = OpenAICompatibleEndpoint(
            descriptor=_descriptor("primary"),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        async def sink(text: str) -> None:
            return None

        response = await endpoint.complete_streaming(_request(), on_token=sink)
        assert response.usage == {"completion_tokens": 42, "prompt_tokens": 7, "total_tokens": 0}


class TestTheCascadeStreams:
    async def test_the_router_streams_when_a_sink_is_supplied(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content).get("stream") is True
            return httpx.Response(200, content=_sse(_delta("one"), _delta("two")))

        seen: list[str] = []

        async def sink(text: str) -> None:
            seen.append(text)

        result = await _router(handler).complete(
            tier=ModelTier.SELF_HOSTED,
            request=_request(),
            prompt=create_redacted_prompt("hello"),
            on_token=sink,
        )

        assert result.outcome is RoutingOutcome.OK
        assert result.served_from == "endpoint"
        assert result.streamed is True
        assert seen == ["one", "two"]
        assert result.content == "onetwo"

    async def test_without_a_sink_the_whole_response_path_is_used_unchanged(self) -> None:
        """`on_token=None` must not change a single byte of the existing behaviour."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert "stream" not in json.loads(request.content), (
                "the non-streaming path must not send `stream`; a provider that honours it would "
                "return SSE to a caller parsing JSON"
            )
            return httpx.Response(
                200,
                json={
                    "model": "test-model",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "whole"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        result = await _router(handler).complete(
            tier=ModelTier.SELF_HOSTED, request=_request(), prompt=create_redacted_prompt("hello")
        )
        assert result.content == "whole"
        assert result.streamed is False

    async def test_a_failed_stream_falls_through_to_the_next_endpoint(self) -> None:
        """The cascade is what streaming must not break."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "primary" in str(request.url):
                return httpx.Response(500, text="upstream is unwell")
            return httpx.Response(200, content=_sse(_delta("from the secondary")))

        seen: list[str] = []

        async def sink(text: str) -> None:
            seen.append(text)

        result = await _router(handler, endpoint_ids=("primary", "secondary")).complete(
            tier=ModelTier.SELF_HOSTED,
            request=_request(),
            prompt=create_redacted_prompt("hello"),
            on_token=sink,
        )

        assert result.outcome is RoutingOutcome.OK
        assert result.endpoint_id == "secondary"
        assert seen == ["from the secondary"]
        # And the fallthrough is recorded rather than hidden.
        assert [a.result for a in result.attempts] == ["error", "success"]
        assert result.degraded is True

    async def test_a_streamed_response_is_cached_and_the_hit_delivers_no_deltas(self) -> None:
        """The `served_from` distinction the whole generation path turns on.

        A cache hit that replayed through the sink would be indistinguishable from a provider call
        at the point where `served_from` is decided, which is precisely the distinction
        `generation_runs.served_from` exists to record.
        """
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, content=_sse(_delta("cached me")))

        redis = _Redis()
        router = _router(handler, redis=redis)
        prompt = create_redacted_prompt("hello")

        first_seen: list[str] = []

        async def first_sink(text: str) -> None:
            first_seen.append(text)

        first = await router.complete(
            tier=ModelTier.SELF_HOSTED, request=_request(), prompt=prompt, on_token=first_sink
        )
        assert first.served_from == "endpoint"
        assert first_seen == ["cached me"]

        second_seen: list[str] = []

        async def second_sink(text: str) -> None:
            second_seen.append(text)

        second = await router.complete(
            tier=ModelTier.SELF_HOSTED, request=_request(), prompt=prompt, on_token=second_sink
        )
        assert second.served_from == "L1_exact"
        assert second.content == "cached me"
        assert second_seen == []
        assert second.usage is None, "a cache hit must not report the original call's token usage"
        assert calls["n"] == 1, "the second call reached the provider, so the cache did not serve it"


class TestTheHostedKeyPathReachesTheProvider:
    """A BYO key must arrive as a bearer token on the streaming path too.

    Exercised over a fake transport rather than a real provider because `.env.example` ships
    placeholder keys by design (credentials.md), so there is no hosted key to call. What can be
    proven without one is that the key the resolver produces reaches the wire — which is the part
    that was never asserted for streaming at all.
    """

    async def test_the_resolved_key_is_sent_as_a_bearer_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The credential VALUE is self-labelling and the scheme prefix is assembled at runtime by
        # `tests/synthetic_secrets.py`. Spelling the scheme beside a value here is refused by
        # `scripts/check-test-credentials.py` (FO-SEC001) — the gate matches on SHAPE rather than
        # sensitivity, deliberately, because a scanner cannot tell the difference and a blocked scan
        # that gets waved through is worse than no scan.
        key = f"{SYNTHETIC_MARKER}-llm-key"
        monkeypatch.setenv("LLM_KEY_TEST", key)
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return httpx.Response(200, content=_sse(_delta("hi")))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        descriptor = _descriptor("hosted", key_ref="test")
        config = TierConfig(
            tiers={ModelTier.HIGH_CODING: TierChain(primary="hosted")},
            endpoints={"hosted": descriptor},
        )
        router = ModelRouter(
            tier_config=config,
            registry=EndpointRegistry.from_config(config, http=http),
            cache=TieredSemanticCache(redis=_Redis()),
            breakers={"hosted": CircuitBreaker()},
            key_resolver=EnvKeyResolver(),
        )

        async def sink(text: str) -> None:
            return None

        result = await router.complete(
            tier=ModelTier.HIGH_CODING,
            request=_request(),
            prompt=create_redacted_prompt("hello"),
            on_token=sink,
        )
        assert result.outcome is RoutingOutcome.OK
        assert seen_headers.get("authorization") == bearer_with(key)

    async def test_an_unresolvable_key_still_sends_no_authorization_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing credential must not put the scheme followed by the word None on the wire."""
        monkeypatch.delenv("LLM_KEY_TEST", raising=False)
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return httpx.Response(200, content=_sse(_delta("hi")))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        descriptor = _descriptor("hosted", key_ref="test")
        config = TierConfig(
            tiers={ModelTier.HIGH_CODING: TierChain(primary="hosted")},
            endpoints={"hosted": descriptor},
        )
        router = ModelRouter(
            tier_config=config,
            registry=EndpointRegistry.from_config(config, http=http),
            cache=TieredSemanticCache(redis=_Redis()),
            breakers={"hosted": CircuitBreaker()},
            key_resolver=EnvKeyResolver(),
        )

        async def sink(text: str) -> None:
            return None

        await router.complete(
            tier=ModelTier.HIGH_CODING,
            request=_request(),
            prompt=create_redacted_prompt("hello"),
            on_token=sink,
        )
        assert "authorization" not in seen_headers
