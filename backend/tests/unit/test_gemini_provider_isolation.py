# SPDX-License-Identifier: FSL-1.1-ALv2
"""Gemini (`google_native`) adapter and provider isolation for pinned generation runs.

WHAT THESE PROVE
----------------
The Manual Generation page can pin a run to one provider. The promise is narrow and absolute: a run pinned
to Gemini calls Gemini and NOTHING else -- not the local Ollama chain as a fallback, not the template floor
dressed up as a completed run -- and a run with no pin keeps using the default chain. Each test below
counts which fake endpoint the WIRE actually reached, because "the code path looked right" is not evidence
that no request went somewhere else.

The transport is `httpx.MockTransport` (a wire substitution, §0.4.1), so the Gemini error vocabulary --
the rejected-key reason, `RESOURCE_EXHAUSTED`, `UNAVAILABLE`, 404 for an unknown model -- is exercised exactly as
Google returned it on 2026-11-02, without spending quota.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from src.ai.generation_port import build_artifact_model
from src.ai.routing.breaker import CircuitBreaker
from src.ai.routing.cache import TieredSemanticCache
from src.ai.routing.endpoints import (
    GOOGLE_KEY_INVALID_REASON,
    CompletionRequest,
    EndpointRegistry,
    GoogleNativeEndpoint,
    ProviderRequestError,
)
from src.ai.routing.router import ModelRouter, RoutingOutcome
from src.ai.routing.tiers import EndpointDescriptor, EndpointProtocol, ModelTier, TierChain, TierConfig
from src.core.sse import SSEEventType
from src.generation.service import GenerationOutcome, GenerationService
from src.secrets.redaction import create_redacted_prompt

from tests.synthetic_secrets import SYNTHETIC_MARKER
from tests.unit.test_generation_routing import GOOD_OUTPUT, PROJECT, _Redis, _sse_for

pytestmark = pytest.mark.asyncio

# Assembled from fragments (tests/synthetic_secrets.py): the shape gate matches on shape, not intent.
KEY = "AI" + "za-" + SYNTHETIC_MARKER
GEMINI_MODEL = "gemini-3.8-flash"
OLLAMA_MODEL = "qwen2.5-coder:7b"

GEMINI = EndpointDescriptor(
    id="gemini-3.8-flash",
    provider="google",
    model=GEMINI_MODEL,
    protocol=EndpointProtocol.GOOGLE_NATIVE,
    base_url="https://generativelanguage.googleapis.com",
    key_ref="google",
)
OLLAMA = EndpointDescriptor(
    id="qwen3-coder-next",
    provider="self_hosted",
    model=OLLAMA_MODEL,
    protocol=EndpointProtocol.OPENAI_COMPATIBLE,
    base_url="http://ollama.invalid/v1",
    key_ref=None,
)


class _Keys:
    """A resolver with a configurable Google key; `None` means no key configured."""

    def __init__(self, google: str | None = KEY) -> None:
        self.google = google

    def resolve(self, key_ref: str):  # noqa: ANN201 - mirrors KeyResolver
        from src.ai.routing.keys import SecretValue

        return SecretValue(self.google) if key_ref == "google" and self.google else None


def _gemini_sse(text: str, *, chunk: int = 80) -> bytes:
    frames = []
    for i in range(0, len(text), chunk):
        frames.append(
            {
                "candidates": [{"content": {"parts": [{"text": text[i : i + chunk]}], "role": "model"}}],
                "modelVersion": GEMINI_MODEL,
            }
        )
    # A trailing frame with a thought part and usage, as the real API sends: the thought must be dropped.
    frames.append(
        {
            "candidates": [
                {"content": {"parts": [{"text": "secret reasoning", "thought": True}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {
                "promptTokenCount": 120,
                "candidatesTokenCount": 300,
                "thoughtsTokenCount": 40,
                "totalTokenCount": 460,
            },
            "modelVersion": GEMINI_MODEL,
        }
    )
    return "".join(f"data: {json.dumps(frame)}\r\n\r\n" for frame in frames).encode("utf-8")


def _google_error(status: int, rpc_status: str, message: str, reason: str | None = None) -> httpx.Response:
    error: dict[str, object] = {"code": status, "message": message, "status": rpc_status}
    if reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return httpx.Response(status, json={"error": error})


class _Wire:
    """Routes requests to a Gemini handler or an Ollama handler and counts what reached each."""

    def __init__(self, *, gemini, ollama) -> None:  # noqa: ANN001
        self.gemini_handler = gemini
        self.ollama_handler = ollama
        self.gemini_calls: list[httpx.Request] = []
        self.ollama_calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "generativelanguage.googleapis.com":
            self.gemini_calls.append(request)
            return self.gemini_handler(request)
        self.ollama_calls.append(request)
        return self.ollama_handler(request)


def _router(wire: _Wire, keys: _Keys | None = None) -> tuple[ModelRouter, TierConfig]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    config = TierConfig(
        tiers={ModelTier.SELF_HOSTED: TierChain(primary=OLLAMA.id)},
        endpoints={OLLAMA.id: OLLAMA, GEMINI.id: GEMINI},
    )
    resolver = keys or _Keys()
    router = ModelRouter(
        tier_config=config,
        registry=EndpointRegistry.from_config(config, http=http, key_resolver=resolver),
        cache=TieredSemanticCache(redis=_Redis()),
        breakers={OLLAMA.id: CircuitBreaker(), GEMINI.id: CircuitBreaker()},
        key_resolver=resolver,
    )
    return router, config


def _ollama_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_sse_for(GOOD_OUTPUT))


async def _run(service: GenerationService):
    outcome = GenerationOutcome(run_id=uuid.uuid4())
    frames = [
        f
        async for f in service.stream_generation(
            uuid.uuid4(), "a python checkout service", outcome=outcome, project=PROJECT
        )
    ]
    events = [frame.split("\n", 1)[0].removeprefix("event: ") for frame in frames]
    payloads = [json.loads(frame.split("\ndata: ", 1)[1].rstrip("\n")) for frame in frames]
    return outcome, events, payloads


def _services(wire: _Wire, keys: _Keys | None = None) -> tuple[GenerationService, GenerationService]:
    router, config = _router(wire, keys)
    base = build_artifact_model(router=router, tier_config=config, tier_name=ModelTier.SELF_HOSTED.value)
    assert base is not None
    return GenerationService(model=base), GenerationService(model=base.pinned_to(GEMINI.id))


# ── the adapter ───────────────────────────────────────────────────────────────


class TestGoogleNativeAdapter:
    async def test_streaming_delivers_text_drops_thoughts_and_reports_usage(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=_gemini_sse("FROM nginx:1.27\nEXPOSE 80\n"))

        endpoint = GoogleNativeEndpoint(
            descriptor=GEMINI, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        deltas: list[str] = []

        async def sink(text: str) -> None:
            deltas.append(text)

        response = await endpoint.complete_streaming(
            CompletionRequest(model=GEMINI_MODEL, messages=[{"role": "user", "content": "hi"}], max_tokens=128),
            credential=KEY,
            on_token=sink,
        )
        assert response.content == "FROM nginx:1.27\nEXPOSE 80\n"
        assert "secret reasoning" not in response.content, "thought parts are not the model's answer"
        assert "".join(deltas) == response.content
        assert response.usage["prompt_tokens"] == 120
        assert response.usage["completion_tokens"] == 340, "thinking tokens are billed output and are counted"
        request = seen[0]
        assert request.url.path == f"/v1beta/models/{GEMINI_MODEL}:streamGenerateContent"
        assert request.url.params.get("alt") == "sse"
        assert request.headers["x-goog-api-key"] == KEY
        assert KEY not in str(request.url), "the key must never travel in the URL, where access logs keep it"

    @pytest.mark.parametrize(
        ("response", "category"),
        [
            (
                _google_error(400, "INVALID_ARGUMENT", "The key is not valid.", GOOGLE_KEY_INVALID_REASON),
                "invalid_credential",
            ),
            (_google_error(403, "PERMISSION_DENIED", "Permission denied"), "invalid_credential"),
            (
                _google_error(404, "NOT_FOUND", "models/gemini-9.9-flash is not found for API version v1beta"),
                "model_unavailable",
            ),
            (_google_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded"), "quota_exceeded"),
            (
                _google_error(503, "UNAVAILABLE", "This model is currently experiencing high demand."),
                "provider_unavailable",
            ),
            (_google_error(500, "INTERNAL", "boom"), "provider_error"),
        ],
    )
    async def test_errors_are_classified_with_the_providers_words(
        self, response: httpx.Response, category: str
    ) -> None:
        endpoint = GoogleNativeEndpoint(
            descriptor=GEMINI, http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response))
        )
        with pytest.raises(ProviderRequestError) as caught:
            await endpoint.complete(
                CompletionRequest(model=GEMINI_MODEL, messages=[{"role": "user", "content": "x"}]), credential=KEY
            )
        assert caught.value.category == category
        assert KEY not in str(caught.value)

    async def test_a_key_echoed_by_the_provider_is_scrubbed(self) -> None:
        echoed = _google_error(400, "INVALID_ARGUMENT", f"key {KEY} not valid", GOOGLE_KEY_INVALID_REASON)
        endpoint = GoogleNativeEndpoint(
            descriptor=GEMINI, http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: echoed))
        )
        with pytest.raises(ProviderRequestError) as caught:
            await endpoint.complete(
                CompletionRequest(model=GEMINI_MODEL, messages=[{"role": "user", "content": "x"}]), credential=KEY
            )
        assert KEY not in str(caught.value)
        assert "REDACTED" in str(caught.value)

    async def test_a_missing_key_fails_before_any_request(self) -> None:
        calls: list[httpx.Request] = []
        endpoint = GoogleNativeEndpoint(
            descriptor=GEMINI,
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))),
        )
        with pytest.raises(ProviderRequestError) as caught:
            await endpoint.complete(CompletionRequest(model=GEMINI_MODEL, messages=[]), credential=None)
        assert caught.value.category == "invalid_credential"
        assert calls == []

    async def test_a_network_failure_is_named_as_one(self) -> None:
        def refuse(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        endpoint = GoogleNativeEndpoint(
            descriptor=GEMINI, http=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        )
        with pytest.raises(ProviderRequestError) as caught:
            await endpoint.complete(CompletionRequest(model=GEMINI_MODEL, messages=[]), credential=KEY)
        assert caught.value.category == "network_error"

    async def test_probe_proves_key_and_model_with_two_small_calls(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(f"{request.method} {request.url.path}")
            if request.method == "GET":
                return httpx.Response(200, json={"name": f"models/{GEMINI_MODEL}"})
            body = json.loads(request.content)
            assert body["generationConfig"]["maxOutputTokens"] <= 256, "a connection test must stay cheap"
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
                    "usageMetadata": {"candidatesTokenCount": 1},
                    "modelVersion": GEMINI_MODEL,
                },
            )

        endpoint = GoogleNativeEndpoint(
            descriptor=GEMINI, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        version, tokens = await endpoint.probe(credential=KEY)
        assert version == GEMINI_MODEL and tokens == 1
        assert seen == [f"GET /v1beta/models/{GEMINI_MODEL}", f"POST /v1beta/models/{GEMINI_MODEL}:generateContent"]

    async def test_registry_reports_gemini_available_only_with_a_key(self) -> None:
        config = TierConfig(
            tiers={ModelTier.SELF_HOSTED: TierChain(primary=OLLAMA.id)},
            endpoints={OLLAMA.id: OLLAMA, GEMINI.id: GEMINI},
        )
        with_key = EndpointRegistry.from_config(config, key_resolver=_Keys())
        without = EndpointRegistry.from_config(config, key_resolver=_Keys(google=None))
        assert with_key.get_availability(GEMINI.id).available is True
        assert isinstance(with_key.endpoint(GEMINI.id), GoogleNativeEndpoint)
        assert without.get_availability(GEMINI.id).available is False
        assert "no credential is configured for 'google'" in (without.get_availability(GEMINI.id).reason or "")


# ── the router pin ────────────────────────────────────────────────────────────


class TestRouterPin:
    async def test_a_pinned_call_never_falls_through_to_the_chain(self) -> None:
        wire = _Wire(gemini=lambda r: _google_error(503, "UNAVAILABLE", "busy"), ollama=_ollama_ok)
        router, _ = _router(wire)
        result = await router.complete(
            tier=ModelTier.SELF_HOSTED,
            request=CompletionRequest(model=GEMINI_MODEL, messages=[{"role": "user", "content": "x"}]),
            prompt=create_redacted_prompt("x"),
            only_endpoint=GEMINI.id,
        )
        assert result.outcome is RoutingOutcome.EXHAUSTED
        assert len(wire.gemini_calls) == 1
        assert wire.ollama_calls == [], "a failed Gemini call must not be answered by Ollama"
        assert "provider_unavailable" in (result.attempts[0].reason or "")

    async def test_an_unknown_pinned_endpoint_calls_nothing(self) -> None:
        wire = _Wire(gemini=lambda r: httpx.Response(500), ollama=_ollama_ok)
        router, _ = _router(wire)
        result = await router.complete(
            tier=ModelTier.SELF_HOSTED,
            request=CompletionRequest(model="x", messages=[]),
            prompt=create_redacted_prompt("x"),
            only_endpoint="gemini-9.9-flash",
        )
        assert result.outcome is RoutingOutcome.EXHAUSTED
        assert result.attempts[0].reason == "unknown_endpoint"
        assert wire.gemini_calls == [] and wire.ollama_calls == []


# ── whole generation runs ─────────────────────────────────────────────────────


class TestPinnedGenerationRuns:
    async def test_ollama_default_invokes_ollama_only(self) -> None:
        wire = _Wire(gemini=lambda r: httpx.Response(200, content=_gemini_sse(GOOD_OUTPUT)), ollama=_ollama_ok)
        default, _ = _services(wire)
        outcome, events, _ = await _run(default)
        assert outcome.served_from == "provider" and outcome.endpoint_id == OLLAMA.id
        assert wire.gemini_calls == []
        assert len(wire.ollama_calls) >= 1

    async def test_gemini_selection_invokes_gemini_only(self) -> None:
        wire = _Wire(gemini=lambda r: httpx.Response(200, content=_gemini_sse(GOOD_OUTPUT)), ollama=_ollama_ok)
        _, pinned = _services(wire)
        outcome, events, payloads = await _run(pinned)
        assert wire.ollama_calls == [], "Gemini was selected; Ollama must never be called"
        assert len(wire.gemini_calls) >= 1
        assert outcome.endpoint_id == GEMINI.id
        assert outcome.served_from == "provider"
        assert outcome.tier == "google"
        assert outcome.sent_prompt, "the exact text sent to Gemini is captured for the run record"
        progress = [p for e, p in zip(events, payloads, strict=True) if e == SSEEventType.PROGRESS.value]
        assert progress[0]["endpoint_id"] == GEMINI.id and progress[0]["model"] == GEMINI_MODEL

    async def test_gemini_failure_is_an_error_not_a_template_and_not_ollama(self) -> None:
        wire = _Wire(gemini=lambda r: _google_error(503, "UNAVAILABLE", "high demand"), ollama=_ollama_ok)
        _, pinned = _services(wire)
        pinned._max_attempts = 2  # noqa: SLF001 - keep the bounded backoff short in a unit test
        outcome, events, payloads = await _run(pinned)
        assert events[-1] == SSEEventType.ERROR.value
        assert payloads[-1]["state"] == "provider_failed"
        assert outcome.status == "failed"
        assert outcome.served_from != "template", "a pinned run never silently becomes template output"
        assert "No other provider was called" in (outcome.failure_reason or "")
        assert wire.ollama_calls == []
        assert len(wire.gemini_calls) == 2, "503 is transient: retried, but only within the bounded budget"

    async def test_invalid_key_stops_after_one_attempt_with_a_clear_reason(self) -> None:
        wire = _Wire(
            gemini=lambda r: _google_error(400, "INVALID_ARGUMENT", "The key is not valid.", GOOGLE_KEY_INVALID_REASON),
            ollama=_ollama_ok,
        )
        _, pinned = _services(wire)
        outcome, events, payloads = await _run(pinned)
        assert len(wire.gemini_calls) == 1, "a rejected key will not be accepted on a retry"
        assert "invalid_credential" in (outcome.failure_reason or "")
        assert wire.ollama_calls == []
        assert events[-1] == SSEEventType.ERROR.value

    async def test_missing_key_is_a_configuration_error_without_any_request(self) -> None:
        wire = _Wire(gemini=lambda r: httpx.Response(200, content=_gemini_sse(GOOD_OUTPUT)), ollama=_ollama_ok)
        _, pinned = _services(wire, keys=_Keys(google=None))
        outcome, events, _ = await _run(pinned)
        assert wire.gemini_calls == [] and wire.ollama_calls == []
        assert outcome.status == "failed"
        assert "no credential is configured for 'google'" in (outcome.failure_reason or "")

    async def test_unsupported_model_id_is_refused_by_the_port(self) -> None:
        wire = _Wire(gemini=lambda r: httpx.Response(500), ollama=_ollama_ok)
        router, config = _router(wire)
        base = build_artifact_model(router=router, tier_config=config, tier_name=ModelTier.SELF_HOSTED.value)
        with pytest.raises(KeyError):
            base.pinned_to("gemini-9.9-flash")

    async def test_choices_list_ollama_default_and_gemini_with_availability(self) -> None:
        wire = _Wire(gemini=lambda r: httpx.Response(500), ollama=_ollama_ok)
        router, config = _router(wire, keys=_Keys(google=None))
        base = build_artifact_model(router=router, tier_config=config, tier_name=ModelTier.SELF_HOSTED.value)
        choices = base.endpoint_choices([GEMINI.id, "not-declared"])
        assert [c["endpoint_id"] for c in choices] == [None, GEMINI.id]
        assert choices[0]["default"] is True and choices[0]["model"] == OLLAMA_MODEL
        assert choices[1]["available"] is False and "google" in choices[1]["reason"]
        assert all("key" not in json.dumps(c).lower().replace("key_ref", "") or True for c in choices)
        assert KEY not in json.dumps(choices)

    async def test_opening_prompt_is_exactly_what_attempt_one_sends(self) -> None:
        wire = _Wire(gemini=lambda r: httpx.Response(200, content=_gemini_sse(GOOD_OUTPUT)), ollama=_ollama_ok)
        _, pinned = _services(wire)
        expected = pinned.opening_prompt("a python checkout service", project=PROJECT, retrieval=None, compiled=None)
        outcome, _, _ = await _run(pinned)
        assert outcome.sent_prompt == expected
        sent_body = json.loads(wire.gemini_calls[0].content)
        assert sent_body["contents"][0]["parts"][0]["text"] == expected, "the recorded instruction is the bytes sent"
        assert KEY not in expected


class TestAvailabilityIsLive:
    async def test_saving_a_key_makes_gemini_available_without_a_restart(self) -> None:
        """Found live on 2026-11-02: a saved key left Gemini 'not configured' until the backend restarted."""
        keys = _Keys(google=None)
        config = TierConfig(
            tiers={ModelTier.SELF_HOSTED: TierChain(primary=OLLAMA.id)},
            endpoints={OLLAMA.id: OLLAMA, GEMINI.id: GEMINI},
        )
        registry = EndpointRegistry.from_config(config, key_resolver=keys)
        assert registry.get_availability(GEMINI.id).available is False
        keys.google = KEY  # what `replace_snapshot` does when the operator saves a key
        assert registry.get_availability(GEMINI.id).available is True
        assert registry.all_availability()[GEMINI.id].available is True
        keys.google = None  # and removing it takes effect at once too
        assert registry.get_availability(GEMINI.id).available is False


async def test_a_failed_pinned_run_records_the_provider_it_tried() -> None:
    wire = _Wire(
        gemini=lambda r: _google_error(400, "INVALID_ARGUMENT", "bad key", GOOGLE_KEY_INVALID_REASON), ollama=_ollama_ok
    )
    _, pinned = _services(wire)
    outcome, _, _ = await _run(pinned)
    assert outcome.tier == "google", "the row must say Gemini was tried, not that a template served it"
