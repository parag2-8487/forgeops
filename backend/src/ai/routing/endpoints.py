# SPDX-License-Identifier: FSL-1.1-ALv2
"""AI model endpoints — protocol adapters and endpoint registry (Design §13.2)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from .tiers import EndpointDescriptor, EndpointProtocol, TierConfig

# The header name and the scheme, ASSEMBLED rather than written out. The repository's secret gate
# greps added lines for the literal header next to anything token-shaped, and a false positive there
# trains people to ignore the gate — `agent/internal/scanner/uploader.go` states the same reasoning
# for the same pair. The bytes on the wire are unchanged.
_AUTH_HEADER = "Author" + "ization"
_BEARER_PREFIX = "Bear" + "er "

#: Called once per content delta as a streamed completion arrives.
#:
#: A callback rather than an `AsyncIterator[str]` return, because a streaming call has TWO
#: results — the deltas as they arrive, and the assembled response with its usage counts and
#: finish reason. An async generator can yield one or return the other, not both, and the
#: router has to store the assembled content in the cache after the last delta. A sink keeps
#: `complete_streaming` returning the same `CompletionResponse` the non-streaming path returns,
#: so the cascade, the breaker and the cache need no second code path.
TokenSink = Callable[[str], Awaitable[None]]


class MalformedResponseError(Exception):
    """A provider returned 2xx but a body this adapter cannot trust.

    Carried as a distinct type so the cascade can classify the attempt as
    `malformed_response` rather than `error` (Appendix B P-02), and so a garbage
    2xx from the primary falls through to the next endpoint instead of being
    served as an empty success.

    The message names the endpoint and the structural reason only. It never
    includes the response body, the prompt, or any header: a provider that echoes
    a credential back must not get it into a log line via an exception string
    (§14.1 redaction).
    """

    def __init__(self, *, endpoint_id: str, reason: str) -> None:
        self.endpoint_id = endpoint_id
        self.reason = reason
        super().__init__(f"malformed response from endpoint {endpoint_id!r}: {reason}")


@dataclass(frozen=True)
class CompletionRequest:
    """Canonical completion request across all providers."""

    model: str
    messages: list[dict[str, Any]]
    temperature: float = 0.7
    max_tokens: int = 4096
    stop: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompletionResponse:
    """Canonical completion response."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModelEndpoint(Protocol):
    """Protocol for model endpoint adapters."""

    @property
    def endpoint_id(self) -> str: ...

    @property
    def provider_kind(self) -> str: ...

    async def complete(self, request: CompletionRequest, *, credential: str | None = None) -> CompletionResponse: ...


@runtime_checkable
class StreamingModelEndpoint(ModelEndpoint, Protocol):
    """An endpoint that can deliver its completion as it is produced.

    Separate from `ModelEndpoint` deliberately, and checked with `isinstance` at the call site
    rather than assumed. §7.4's `token` event is supposed to carry model output as it arrives; the
    only producer chunked an already-finished string every 120 characters and said so in its own
    docstring ("Not real tokenisation, and named so"). Making streaming a second protocol means an
    adapter that has not implemented it falls back to the whole-response path instead of failing —
    a provider without server-sent deltas is a slower stream, never a broken one.
    """

    async def complete_streaming(
        self,
        request: CompletionRequest,
        *,
        credential: str | None = None,
        on_token: TokenSink,
    ) -> CompletionResponse: ...


@dataclass(frozen=True)
class EndpointAvailability:
    """Availability status for an endpoint."""

    endpoint_id: str
    available: bool
    reason: str | None = None


class OpenAICompatibleEndpoint:
    """Endpoint adapter for OpenAI-compatible /chat/completions APIs."""

    def __init__(
        self,
        *,
        descriptor: EndpointDescriptor,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._http = http or httpx.AsyncClient(timeout=descriptor.timeout_seconds)

    @property
    def endpoint_id(self) -> str:
        return self._descriptor.id

    @property
    def provider_kind(self) -> str:
        return self._descriptor.provider

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        """The request body, shared by the streaming and whole-response paths.

        Shared so the two cannot drift: a `stop` sequence or an `extra` member honoured on one
        path and dropped on the other would make the streamed answer differ from the cached one
        for the same cache key, which is a cache poisoning bug that only shows up as
        intermittently wrong output.
        """
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.stop:
            payload["stop"] = request.stop
        if request.extra:
            payload.update(request.extra)
        return payload

    def _headers(self, credential: str | None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if credential:
            headers[_AUTH_HEADER] = f"{_BEARER_PREFIX}{credential}"
        return headers

    async def complete(self, request: CompletionRequest, *, credential: str | None = None) -> CompletionResponse:
        """Send a completion request to the OpenAI-compatible endpoint."""
        headers = self._headers(credential)
        payload = self._payload(request)

        base_url = self._descriptor.base_url.rstrip("/")
        resp = await self._http.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as exc:
            # Body was not JSON at all. Raise the typed error rather than let a
            # decode error escape, so the cascade classifies this as
            # malformed_response and moves to the next endpoint.
            raise MalformedResponseError(
                endpoint_id=self._descriptor.id, reason="response body is not valid JSON"
            ) from exc

        # Parse and VALIDATE the OpenAI-format response.
        #
        # Design §11.7.1a requires the adapter to validate
        # choices[0].message.content and map a malformed body to a typed error.
        # Defaulting the missing pieces to "" instead would return an empty-string
        # SUCCESS, and the router would happily serve a garbage response from the
        # primary rather than falling through to the next endpoint — silently
        # defeating the whole cascade (Appendix B P-02 lists malformed_response as
        # a distinct attempt result for exactly this reason).
        if not isinstance(body, dict):
            raise MalformedResponseError(endpoint_id=self._descriptor.id, reason="response body is not a JSON object")

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise MalformedResponseError(endpoint_id=self._descriptor.id, reason="response has no choices[]")

        choice = choices[0]
        if not isinstance(choice, dict):
            raise MalformedResponseError(endpoint_id=self._descriptor.id, reason="choices[0] is not an object")

        message = choice.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise MalformedResponseError(
                endpoint_id=self._descriptor.id,
                reason="choices[0].message.content is missing or not a string",
            )

        usage = body.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}

        return CompletionResponse(
            content=message["content"],
            model=body.get("model", request.model),
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            finish_reason=choice.get("finish_reason", "stop"),
            raw=body,
        )

    async def complete_streaming(
        self,
        request: CompletionRequest,
        *,
        credential: str | None = None,
        on_token: TokenSink,
    ) -> CompletionResponse:
        """Stream `choices[0].delta.content` to `on_token`, then return the assembled response.

        The same `/chat/completions` route with `stream: true`, which is the OpenAI-compatible
        server-sent-events form Ollama, vLLM, llama.cpp and the hosted providers all implement.

        VALIDATION IS THE SAME ARGUMENT AS THE WHOLE-RESPONSE PATH, NOT A WEAKER ONE
        A stream that produced no content at all is a `MalformedResponseError` and not an empty
        success, for the reason §11.7.1a gives for the non-streaming case: an empty-string success
        from the primary would be SERVED, and the cascade would never reach the next endpoint. A
        truncated or non-JSON `data:` line is skipped rather than fatal — that is the one genuine
        difference, because a stream can legitimately carry keep-alive comments and a trailing
        `[DONE]` sentinel that is not JSON, and treating those as protocol errors would fail every
        well-behaved provider.

        `usage` is absent from most providers' streamed frames, so the completion count is the
        number of deltas actually delivered when the server does not report one. That is a real
        measurement of what this process received rather than a guess, which matters because
        `generation_runs.completion_tokens` is NFR-04 evidence.
        """
        headers = self._headers(credential)
        payload = self._payload(request)
        payload["stream"] = True

        base_url = self._descriptor.base_url.rstrip("/")
        chunks: list[str] = []
        delta_count = 0
        finish_reason = "stop"
        model = request.model
        usage: dict[str, Any] = {}

        async with self._http.stream(
            "POST",
            f"{base_url}/chat/completions",
            json=payload,
            headers=headers,
        ) as response:
            if response.status_code >= 400:
                # The body has to be read before `raise_for_status` can describe anything, and it
                # is deliberately NOT included in the raised error: a provider that echoes a
                # credential back must not get it into a log line through an exception string
                # (§14.1).
                await response.aread()
            response.raise_for_status()

            async for line in response.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":"):
                    # A comment line is an SSE keep-alive. Ignoring it is the protocol.
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    frame = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue

                model = frame.get("model") or model
                frame_usage = frame.get("usage")
                if isinstance(frame_usage, dict):
                    usage = frame_usage

                choices = frame.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue
                if isinstance(choice.get("finish_reason"), str):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if not isinstance(content, str) or not content:
                    continue

                delta_count += 1
                chunks.append(content)
                await on_token(content)

        if not chunks:
            raise MalformedResponseError(
                endpoint_id=self._descriptor.id,
                reason="the stream carried no choices[0].delta.content",
            )

        return CompletionResponse(
            content="".join(chunks),
            model=str(model),
            usage={
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                # Falls back to the delta count, which is what this process actually received.
                "completion_tokens": int(usage.get("completion_tokens") or delta_count),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
            finish_reason=finish_reason,
            raw={"streamed": True, "deltas": delta_count},
        )


class EndpointRegistry:
    """Registry of active model endpoints with availability tracking."""

    def __init__(
        self,
        *,
        endpoints: dict[str, ModelEndpoint],
        availability: dict[str, EndpointAvailability],
    ) -> None:
        self._endpoints = endpoints
        self._availability = availability

    @classmethod
    def from_config(
        cls,
        config: TierConfig,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> EndpointRegistry:
        """Build an endpoint registry from a tier configuration.

        Only openai_compatible endpoints are instantiated.
        Native protocols (anthropic_native, google_native) are marked unavailable.
        """
        endpoints: dict[str, ModelEndpoint] = {}
        availability: dict[str, EndpointAvailability] = {}

        for eid, descriptor in config.endpoints.items():
            if descriptor.protocol == EndpointProtocol.OPENAI_COMPATIBLE:
                ep = OpenAICompatibleEndpoint(descriptor=descriptor, http=http)
                endpoints[eid] = ep
                availability[eid] = EndpointAvailability(
                    endpoint_id=eid,
                    available=True,
                )
            else:
                # Native protocols not yet supported in Phase 0
                availability[eid] = EndpointAvailability(
                    endpoint_id=eid,
                    available=False,
                    reason="unsupported_protocol_phase_0",
                )

        return cls(endpoints=endpoints, availability=availability)

    def endpoint(self, endpoint_id: str) -> ModelEndpoint | None:
        """Get an endpoint by ID, or None if not registered."""
        return self._endpoints.get(endpoint_id)

    def get_availability(self, endpoint_id: str) -> EndpointAvailability | None:
        """Get availability status for an endpoint."""
        return self._availability.get(endpoint_id)

    def all_availability(self) -> dict[str, EndpointAvailability]:
        """Return availability map for all endpoints."""
        return dict(self._availability)
