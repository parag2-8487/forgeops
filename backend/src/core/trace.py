# SPDX-License-Identifier: FSL-1.1-ALv2
"""W3C Trace Context primitives and middleware (design.md §4.3, §7.8).

Parse/validate traceparent, preserve tracestate verbatim, mint child span ids,
inject outbound headers, contextvar accessors, emit traceresponse, and a NoopTracer.
Malformed inbound traceparent starts a fresh trace and is never forwarded.
No OTel SDK dependency.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging import span_id_var, trace_id_var

# W3C traceparent format: version-trace_id-span_id-flags
# version: 2 hex chars; trace_id: 32 hex chars; span_id: 16 hex chars; flags: 2 hex chars
_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

# Invalid trace/span IDs (all zeros)
_INVALID_TRACE_ID = "0" * 32
_INVALID_SPAN_ID = "0" * 16


@dataclass(frozen=True)
class TraceContext:
    """Parsed W3C Trace Context."""

    trace_id: str  # 32 hex characters
    parent_span_id: str  # 16 hex characters (from inbound traceparent)
    span_id: str  # 16 hex characters (freshly minted for this span)
    flags: str  # 2 hex characters
    tracestate: str  # Preserved verbatim from inbound headers


# Context variable for the current trace context
_trace_context_var: ContextVar[TraceContext | None] = ContextVar("trace_context", default=None)


def current_trace_context() -> TraceContext | None:
    """Get the current trace context from the contextvar."""
    return _trace_context_var.get()


def current_trace_id() -> str:
    """Get the current trace ID or empty string."""
    ctx = _trace_context_var.get()
    return ctx.trace_id if ctx else ""


def current_span_id() -> str:
    """Get the current span ID or empty string."""
    ctx = _trace_context_var.get()
    return ctx.span_id if ctx else ""


def _mint_span_id() -> str:
    """Generate a random 16-hex-character span ID."""
    return secrets.token_hex(8)


def _mint_trace_id() -> str:
    """Generate a random 32-hex-character trace ID."""
    return secrets.token_hex(16)


def parse_traceparent(header: str | None) -> tuple[str, str, str, str] | None:
    """Parse and validate a traceparent header.

    Returns (version, trace_id, span_id, flags) or None if malformed.
    Malformed values are never forwarded (W3C spec).
    """
    if not header:
        return None
    header = header.strip().lower()
    m = _TRACEPARENT_RE.match(header)
    if not m:
        return None
    version, trace_id, span_id, flags = m.groups()
    # All-zero trace or span IDs are invalid
    if trace_id == _INVALID_TRACE_ID or span_id == _INVALID_SPAN_ID:
        return None
    # Version 255 (ff) is invalid per the spec
    if version == "ff":
        return None
    return version, trace_id, span_id, flags


def create_trace_context(
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> TraceContext:
    """Create a TraceContext from inbound headers.

    Valid traceparent: preserves trace_id, uses original span_id as parent,
    mints a new span_id for this request.

    Malformed/absent traceparent: starts a fresh trace (new trace_id and span_id).
    The malformed value is never forwarded.
    """
    parsed = parse_traceparent(traceparent)
    if parsed is not None:
        _version, trace_id, parent_span_id, flags = parsed
        return TraceContext(
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            span_id=_mint_span_id(),
            flags=flags,
            tracestate=tracestate or "",
        )
    # Fresh trace
    return TraceContext(
        trace_id=_mint_trace_id(),
        parent_span_id="",
        span_id=_mint_span_id(),
        flags="01",  # sampled by default
        tracestate="",  # Never forward tracestate from a malformed traceparent
    )


def format_traceparent(ctx: TraceContext) -> str:
    """Format a TraceContext as a traceparent header value."""
    return f"00-{ctx.trace_id}-{ctx.span_id}-{ctx.flags}"


def format_traceresponse(ctx: TraceContext) -> str:
    """Format a TraceContext as a traceresponse header value."""
    return f"00-{ctx.trace_id}-{ctx.span_id}-{ctx.flags}"


def inject_outbound_headers(ctx: TraceContext, headers: MutableMapping[str, str]) -> None:
    """Inject trace context headers for outbound requests."""
    # Mint a new child span for the outbound call
    child_span = _mint_span_id()
    headers["traceparent"] = f"00-{ctx.trace_id}-{child_span}-{ctx.flags}"
    if ctx.tracestate:
        headers["tracestate"] = ctx.tracestate


class NoopTracer:
    """Phase 0 tracer — propagates context but records nothing.

    Phase 3 swaps in the OTel SDK behind the same interface.
    """

    def start_span(self, name: str, **kwargs: Any) -> NoopSpan:
        return NoopSpan()


class NoopSpan:
    """A no-op span that does nothing."""

    def end(self) -> None:
        pass

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def __enter__(self) -> NoopSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Parse inbound traceparent/tracestate, seed contextvars, emit traceresponse.

    Sits at position 3 in the middleware stack (§4.3):
    ServerError -> RequestId -> TraceContext -> AccessLog -> CORS
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        traceparent = request.headers.get("traceparent")
        tracestate = request.headers.get("tracestate")

        ctx = create_trace_context(traceparent, tracestate)
        token = _trace_context_var.set(ctx)
        # Also set the logging contextvars
        trace_token = trace_id_var.set(ctx.trace_id)
        span_token = span_id_var.set(ctx.span_id)

        try:
            response = await call_next(request)
            # Emit traceresponse header
            response.headers["traceresponse"] = format_traceresponse(ctx)
            return response
        finally:
            _trace_context_var.reset(token)
            trace_id_var.reset(trace_token)
            span_id_var.reset(span_token)
