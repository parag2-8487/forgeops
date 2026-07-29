# SPDX-License-Identifier: FSL-1.1-ALv2
"""AI model endpoints — protocol adapters and endpoint registry (Design §13.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from .tiers import EndpointDescriptor, EndpointProtocol, TierConfig


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

    async def complete(self, request: CompletionRequest, *, api_key: str | None = None) -> CompletionResponse: ...


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

    async def complete(self, request: CompletionRequest, *, api_key: str | None = None) -> CompletionResponse:
        """Send a completion request to the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

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
