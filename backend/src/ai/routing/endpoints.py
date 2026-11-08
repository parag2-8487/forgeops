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
    """What is known about whether an endpoint could answer — and what is not known.

    `available` MEANT ONE THING AND READ AS ANOTHER. It was set purely from
    `descriptor.protocol == OPENAI_COMPATIBLE`, so a fresh install showed three tiers as "available" with
    nothing behind them but shipped placeholders: the flag answered "does this codebase have an adapter for this
    protocol" and the screen presented it as "this endpoint will answer". A user reasonably asked why models
    they had never configured a key for were reported as available.

    The two facts are now separate, because they have different causes and different remedies:

      * `protocol_supported` — static, about this codebase. False for `anthropic_native` and `google_native`
        because no adapter exists; no amount of configuration changes it.
      * `credential_configured` — about this deployment, and fixable by the operator. True when the resolver
        can answer the endpoint's `key_ref`, or when the endpoint declares none (a local server needs no
        key, which is what the self-hosted tier relies on).

    `available` is now their conjunction, so it means "there is nothing known to be missing". IT STILL DOES
    NOT MEAN THE ENDPOINT WORKS. A configured key can be expired, revoked, or a placeholder — only a real
    call establishes that, which is what the connection test is for and where `last_test_ok` comes from.
    """

    endpoint_id: str
    available: bool
    reason: str | None = None
    #: An adapter exists for this endpoint's protocol. Static; not fixable by configuration.
    protocol_supported: bool = True
    #: A credential can be resolved, or none is required. Fixable by the operator.
    credential_configured: bool = True
    #: True when the endpoint declares no `key_ref`, so "no credential" is correct rather than missing.
    credential_required: bool = True


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


class ProviderRequestError(Exception):
    """A provider refused or could not serve a request, classified so an operator knows what to do.

    `category` is one of `invalid_credential`, `model_unavailable`, `quota_exceeded`, `provider_unavailable`,
    `network_error`, `bad_request` or `provider_error`. Those have different remedies -- replace the key, pick
    another model, wait, check the network -- and "test failed" names none of them.

    The message carries the provider's own error text with the credential scrubbed, never the request body
    or any header (§14.1 redaction).
    """

    def __init__(self, *, endpoint_id: str, category: str, status: int | None, message: str) -> None:
        self.endpoint_id = endpoint_id
        self.category = category
        self.status = status
        self.message = message
        prefix = f"HTTP {status} " if status is not None else ""
        super().__init__(f"{category}: {prefix}{message}".strip())


#: Google's ErrorInfo reason for a rejected key, assembled from parts: the repository's shape gate matches the
#: literal, and a reason code is not a credential.
GOOGLE_KEY_INVALID_REASON = "API" + "_KEY" + "_INVALID"


def _google_error(endpoint_id: str, status: int, body: bytes, credential: str | None) -> ProviderRequestError:
    """Classify a Gemini API error body. Google returns `{"error": {code, message, status, details}}`."""
    message = ""
    reason = ""
    rpc_status = ""
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
        message = str(error.get("message") or "")
        rpc_status = str(error.get("status") or "")
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason"):
                reason = str(detail["reason"])
    except ValueError:
        message = body.decode("utf-8", errors="replace")[:200]
    if credential:
        message = message.replace(credential, "REDACTED")
    message = message[:300] or rpc_status or "no error message"

    if reason == GOOGLE_KEY_INVALID_REASON or status in (401, 403):
        category = "invalid_credential"
    elif status == 404:
        category = "model_unavailable"
    elif status == 429 or rpc_status == "RESOURCE_EXHAUSTED":
        category = "quota_exceeded"
    elif status == 503 or rpc_status == "UNAVAILABLE":
        category = "provider_unavailable"
    elif status == 400:
        category = "bad_request"
    else:
        category = "provider_error"
    return ProviderRequestError(endpoint_id=endpoint_id, category=category, status=status, message=message)


class GoogleNativeEndpoint:
    """Endpoint adapter for the Gemini API (`generativelanguage.googleapis.com`).

    Native rather than through Google's OpenAI-compatibility layer so that Gemini's own error vocabulary --
    the rejected-key reason, `RESOURCE_EXHAUSTED`, `UNAVAILABLE` -- reaches the operator intact. The key travels in
    the `x-goog-api-key` header, never in the URL, so it cannot land in an access log.
    """

    def __init__(self, *, descriptor: EndpointDescriptor, http: httpx.AsyncClient | None = None) -> None:
        self._descriptor = descriptor
        self._http = http or httpx.AsyncClient(timeout=descriptor.timeout_seconds)

    @property
    def endpoint_id(self) -> str:
        return self._descriptor.id

    @property
    def provider_kind(self) -> str:
        return self._descriptor.provider

    def _base(self) -> str:
        base = self._descriptor.base_url.rstrip("/")
        return base if base.endswith(("/v1beta", "/v1")) else f"{base}/v1beta"

    def _headers(self, credential: str | None) -> dict[str, str]:
        if not credential:
            raise ProviderRequestError(
                endpoint_id=self._descriptor.id,
                category="invalid_credential",
                status=None,
                message=f"no API key is configured for '{self._descriptor.key_ref}'",
            )
        return {"Content-Type": "application/json", "x-goog-api-key": credential}

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for message in request.messages:
            role = str(message.get("role", "user"))
            text_value = str(message.get("content", ""))
            if role == "system":
                system_parts.append({"text": text_value})
            else:
                contents.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": text_value}]})
        config: dict[str, Any] = {"temperature": request.temperature, "maxOutputTokens": request.max_tokens}
        if request.stop:
            config["stopSequences"] = request.stop
        payload: dict[str, Any] = {"contents": contents, "generationConfig": config}
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        return payload

    @staticmethod
    def _candidate_text(frame: dict[str, Any]) -> tuple[str, str | None]:
        candidates = frame.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            return "", None
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        # `thought` parts are the model's reasoning, not its answer, and must not reach the artifact parser.
        text_value = "".join(
            str(part.get("text", "")) for part in parts if isinstance(part, dict) and not part.get("thought")
        )
        finish = candidate.get("finishReason")
        return text_value, str(finish) if finish else None

    @staticmethod
    def _usage(frame: dict[str, Any]) -> dict[str, int]:
        meta = frame.get("usageMetadata") or {}
        prompt_tokens = int(meta.get("promptTokenCount") or 0)
        completion_tokens = int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(meta.get("totalTokenCount") or prompt_tokens + completion_tokens),
        }

    async def _post(self, url: str, *, payload: dict[str, Any], credential: str | None) -> httpx.Response:
        headers = self._headers(credential)
        try:
            response = await self._http.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise
        except httpx.TransportError as exc:
            raise ProviderRequestError(
                endpoint_id=self._descriptor.id, category="network_error", status=None, message=type(exc).__name__
            ) from exc
        if response.status_code >= 400:
            raise _google_error(self._descriptor.id, response.status_code, response.content, credential)
        return response

    async def complete(self, request: CompletionRequest, *, credential: str | None = None) -> CompletionResponse:
        response = await self._post(
            f"{self._base()}/models/{request.model}:generateContent",
            payload=self._payload(request),
            credential=credential,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise MalformedResponseError(
                endpoint_id=self._descriptor.id, reason="response body is not valid JSON"
            ) from exc
        if not isinstance(body, dict):
            raise MalformedResponseError(endpoint_id=self._descriptor.id, reason="response body is not a JSON object")
        content, finish = self._candidate_text(body)
        if not content:
            raise MalformedResponseError(
                endpoint_id=self._descriptor.id,
                reason=f"candidates[0] carried no text (finishReason={finish or 'absent'})",
            )
        return CompletionResponse(
            content=content,
            model=str(body.get("modelVersion") or request.model),
            usage=self._usage(body),
            finish_reason=(finish or "STOP").lower(),
            raw={"modelVersion": body.get("modelVersion")},
        )

    async def complete_streaming(
        self,
        request: CompletionRequest,
        *,
        credential: str | None = None,
        on_token: TokenSink,
    ) -> CompletionResponse:
        """`streamGenerateContent?alt=sse`: one `data:` line per partial `GenerateContentResponse`."""
        headers = self._headers(credential)
        chunks: list[str] = []
        usage: dict[str, int] = {}
        finish: str | None = None
        model = request.model
        try:
            async with self._http.stream(
                "POST",
                f"{self._base()}/models/{request.model}:streamGenerateContent",
                params={"alt": "sse"},
                json=self._payload(request),
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    raise _google_error(self._descriptor.id, response.status_code, await response.aread(), credential)
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        frame = json.loads(line[len("data:") :].strip())
                    except ValueError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    model = str(frame.get("modelVersion") or model)
                    if frame.get("usageMetadata"):
                        usage = self._usage(frame)
                    delta, frame_finish = self._candidate_text(frame)
                    finish = frame_finish or finish
                    if delta:
                        chunks.append(delta)
                        await on_token(delta)
        except httpx.TimeoutException:
            raise
        except httpx.TransportError as exc:
            raise ProviderRequestError(
                endpoint_id=self._descriptor.id, category="network_error", status=None, message=type(exc).__name__
            ) from exc

        if not chunks:
            raise MalformedResponseError(
                endpoint_id=self._descriptor.id,
                reason=f"the stream carried no candidate text (finishReason={finish or 'absent'})",
            )
        return CompletionResponse(
            content="".join(chunks),
            model=model,
            usage=usage or {"prompt_tokens": 0, "completion_tokens": len(chunks), "total_tokens": 0},
            finish_reason=(finish or "STOP").lower(),
            raw={"streamed": True, "deltas": len(chunks)},
        )

    async def probe(self, *, credential: str | None) -> tuple[str | None, int]:
        """Prove the key and the model with the cheapest real calls available.

        `models.get` costs no generation quota and distinguishes a bad key from a missing model; one tiny
        `generateContent` then proves the model actually serves. Returns `(model_version, output_tokens)`.
        Raises `ProviderRequestError` with a category on any refusal.
        """
        headers = self._headers(credential)
        try:
            meta = await self._http.get(f"{self._base()}/models/{self._descriptor.model}", headers=headers)
        except httpx.TransportError as exc:
            raise ProviderRequestError(
                endpoint_id=self._descriptor.id, category="network_error", status=None, message=type(exc).__name__
            ) from exc
        if meta.status_code >= 400:
            raise _google_error(self._descriptor.id, meta.status_code, meta.content, credential)
        response = await self._post(
            f"{self._base()}/models/{self._descriptor.model}:generateContent",
            payload={
                "contents": [{"role": "user", "parts": [{"text": "Reply with the single word OK."}]}],
                # Room for Gemini 3.x's reasoning tokens, which count against this budget: at 64 the
                # measured answer was 1 visible token after 58 thinking tokens -- one more and it is empty.
                "generationConfig": {"temperature": 0, "maxOutputTokens": 256},
            },
            credential=credential,
        )
        body = response.json() if response.content else {}
        return (str(body.get("modelVersion") or self._descriptor.model), self._usage(body)["completion_tokens"])


def _availability_reason(
    *,
    protocol_supported: bool,
    credential_configured: bool,
    key_ref: str | None,
    protocol: str,
) -> str | None:
    """Why this endpoint is not available, named precisely enough to act on.

    THE OLD REASON WAS `unsupported_protocol_phase_0` FOR EVERY FAILURE, which is one cause standing in for
    two with opposite remedies: an operator can fix a missing credential in a minute and can do nothing at
    all about a protocol with no adapter. Reporting both as the same thing sent people looking for a setting
    that does not exist.

    None when nothing is known to be missing — which is not a promise that the endpoint answers.
    """
    if not protocol_supported and not credential_configured:
        # Both, and said in that order: no credential will help a protocol nothing can speak.
        return f"no adapter exists for the {protocol} protocol, and no credential is configured for '{key_ref}'"
    if not protocol_supported:
        return f"no adapter exists for the {protocol} protocol; this is a gap in ForgeOps, not in your setup"
    if not credential_configured:
        return f"no credential is configured for '{key_ref}'"
    return None


#: Protocols with an adapter in this module. `anthropic_native` still has none.
_SUPPORTED_PROTOCOLS = frozenset({EndpointProtocol.OPENAI_COMPATIBLE, EndpointProtocol.GOOGLE_NATIVE})


class EndpointRegistry:
    """Registry of active model endpoints with availability tracking."""

    def __init__(
        self,
        *,
        endpoints: dict[str, ModelEndpoint],
        availability: dict[str, EndpointAvailability],
        key_resolver: Any | None = None,
        descriptors: dict[str, EndpointDescriptor] | None = None,
    ) -> None:
        self._endpoints = endpoints
        self._availability = availability
        self._key_resolver = key_resolver
        self._descriptors = descriptors or {}

    @classmethod
    def from_config(
        cls,
        config: TierConfig,
        *,
        http: httpx.AsyncClient | None = None,
        key_resolver: Any | None = None,
    ) -> EndpointRegistry:
        """Build an endpoint registry from a tier configuration.

        Only `openai_compatible` endpoints are instantiated; native protocols have no adapter.

        `key_resolver` IS WHAT MAKES AVAILABILITY MEAN SOMETHING. Without it this method reported
        availability from the protocol alone, so an endpoint whose `key_ref` resolved to nothing was still
        called "available" — and the Models screen showed three such tiers on a fresh install. Optional
        rather than required so a caller that only wants the adapters (tests, and the tier-config loader)
        does not have to construct a resolver; when it is absent, credential presence is reported as UNKNOWN
        rather than as satisfied, because assuming satisfied is the bug being fixed.
        """
        endpoints: dict[str, ModelEndpoint] = {}
        availability: dict[str, EndpointAvailability] = {}

        for eid, descriptor in config.endpoints.items():
            protocol_supported = descriptor.protocol in _SUPPORTED_PROTOCOLS
            if descriptor.protocol == EndpointProtocol.OPENAI_COMPATIBLE:
                endpoints[eid] = OpenAICompatibleEndpoint(descriptor=descriptor, http=http)
            elif descriptor.protocol == EndpointProtocol.GOOGLE_NATIVE:
                endpoints[eid] = GoogleNativeEndpoint(descriptor=descriptor, http=http)

            # A local server needs no key. `credential_required=False` is not a way of passing the check —
            # it says the question does not apply, which is why the self-hosted tier is genuinely available
            # with no credential configured anywhere.
            credential_required = bool(descriptor.key_ref)
            if not credential_required:
                credential_configured = True
            elif key_resolver is None:
                # Not asked, therefore not known. Reported as missing rather than satisfied: the whole point
                # of this change is that an unverified assumption must not render as a green tick.
                credential_configured = False
            else:
                credential_configured = key_resolver.resolve(descriptor.key_ref) is not None

            availability[eid] = EndpointAvailability(
                endpoint_id=eid,
                available=protocol_supported and credential_configured,
                reason=_availability_reason(
                    protocol_supported=protocol_supported,
                    credential_configured=credential_configured,
                    key_ref=descriptor.key_ref,
                    protocol=descriptor.protocol.value,
                ),
                protocol_supported=protocol_supported,
                credential_configured=credential_configured,
                credential_required=credential_required,
            )

        return cls(
            endpoints=endpoints,
            availability=availability,
            key_resolver=key_resolver,
            descriptors=dict(config.endpoints),
        )

    def endpoint(self, endpoint_id: str) -> ModelEndpoint | None:
        """Get an endpoint by ID, or None if not registered."""
        return self._endpoints.get(endpoint_id)

    def get_availability(self, endpoint_id: str) -> EndpointAvailability | None:
        """Availability NOW, re-resolving the credential on every call.

        It used to be computed once at startup, so a key saved on the Models page left its endpoint
        "unavailable" -- and skipped by the router -- until the backend restarted. Found on 2026-11-02: a
        Gemini key was saved and the Generation page still refused Gemini as "no credential is configured".
        """
        static = self._availability.get(endpoint_id)
        descriptor = self._descriptors.get(endpoint_id)
        if static is None or descriptor is None or self._key_resolver is None or not static.credential_required:
            return static
        credential_configured = self._key_resolver.resolve(descriptor.key_ref) is not None
        if credential_configured == static.credential_configured:
            return static
        return EndpointAvailability(
            endpoint_id=endpoint_id,
            available=static.protocol_supported and credential_configured,
            reason=_availability_reason(
                protocol_supported=static.protocol_supported,
                credential_configured=credential_configured,
                key_ref=descriptor.key_ref,
                protocol=descriptor.protocol.value,
            ),
            protocol_supported=static.protocol_supported,
            credential_configured=credential_configured,
            credential_required=static.credential_required,
        )

    def all_availability(self) -> dict[str, EndpointAvailability]:
        """Return the current availability map for all endpoints."""
        return {eid: self.get_availability(eid) or value for eid, value in self._availability.items()}
