# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for src/core/trace.py — W3C Trace Context.

P-13 focused examples: valid preservation, child-span replacement, malformed reset,
and exact tracestate pass-through.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.core.trace import (
    NoopTracer,
    TraceContextMiddleware,
    create_trace_context,
    inject_outbound_headers,
    parse_traceparent,
)


class TestParseTraceparent:
    """parse_traceparent validates the W3C format."""

    def test_valid_header(self):
        result = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        assert result is not None
        version, trace_id, span_id, flags = result
        assert version == "00"
        assert trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert span_id == "00f067aa0ba902b7"
        assert flags == "01"

    def test_none_returns_none(self):
        assert parse_traceparent(None) is None

    def test_empty_returns_none(self):
        assert parse_traceparent("") is None

    def test_malformed_returns_none(self):
        assert parse_traceparent("not-a-valid-traceparent") is None

    def test_short_trace_id_returns_none(self):
        assert parse_traceparent("00-abc-00f067aa0ba902b7-01") is None

    def test_all_zero_trace_id_invalid(self):
        assert parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01") is None

    def test_all_zero_span_id_invalid(self):
        assert parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01") is None

    def test_version_ff_invalid(self):
        assert parse_traceparent("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01") is None

    def test_uppercase_normalized(self):
        """Case-insensitive parsing: uppercase hex is accepted."""
        result = parse_traceparent("00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01")
        assert result is not None


class TestCreateTraceContext:
    """create_trace_context: valid preservation and malformed reset."""

    def test_valid_traceparent_preserves_trace_id(self):
        """Valid trace-id is preserved; span-id is freshly minted."""
        ctx = create_trace_context(traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert ctx.parent_span_id == "00f067aa0ba902b7"
        # New span must differ from parent
        assert ctx.span_id != "00f067aa0ba902b7"
        assert len(ctx.span_id) == 16
        assert ctx.flags == "01"

    def test_malformed_traceparent_starts_fresh_trace(self):
        """Malformed inbound traceparent => start a fresh trace."""
        ctx = create_trace_context(traceparent="garbage-value")
        assert len(ctx.trace_id) == 32
        assert len(ctx.span_id) == 16
        assert ctx.parent_span_id == ""
        # Fresh trace should not forward the malformed tracestate either
        assert ctx.tracestate == ""

    def test_absent_traceparent_starts_fresh(self):
        """No traceparent => fresh trace."""
        ctx = create_trace_context(traceparent=None)
        assert len(ctx.trace_id) == 32
        assert len(ctx.span_id) == 16

    def test_tracestate_preserved_verbatim(self):
        """tracestate is passed through unmodified when traceparent is valid."""
        tracestate = "congo=t61rcWkgMzE,rojo=00f067aa0ba902b7"
        ctx = create_trace_context(
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            tracestate=tracestate,
        )
        assert ctx.tracestate == tracestate

    def test_tracestate_not_forwarded_on_malformed(self):
        """Malformed traceparent means tracestate is also dropped."""
        ctx = create_trace_context(
            traceparent="invalid",
            tracestate="congo=t61rcWkgMzE",
        )
        assert ctx.tracestate == ""


class TestOutboundHeaders:
    """inject_outbound_headers mints child spans."""

    def test_injects_traceparent(self):
        ctx = create_trace_context(traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        headers: dict[str, str] = {}
        inject_outbound_headers(ctx, headers)
        assert "traceparent" in headers
        parts = headers["traceparent"].split("-")
        assert parts[0] == "00"
        assert parts[1] == "4bf92f3577b34da6a3ce929d0e0e4736"  # trace_id preserved
        assert parts[2] != ctx.span_id  # new child span for outbound
        assert len(parts[2]) == 16
        assert parts[3] == "01"

    def test_injects_tracestate_when_present(self):
        ctx = create_trace_context(
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            tracestate="vendor=value",
        )
        headers: dict[str, str] = {}
        inject_outbound_headers(ctx, headers)
        assert headers["tracestate"] == "vendor=value"

    def test_no_tracestate_when_empty(self):
        ctx = create_trace_context(
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        )
        headers: dict[str, str] = {}
        inject_outbound_headers(ctx, headers)
        assert "tracestate" not in headers


class TestTraceContextMiddleware:
    """Middleware integration tests."""

    @pytest.fixture()
    def app(self) -> FastAPI:
        app = FastAPI()
        app.add_middleware(TraceContextMiddleware)

        @app.get("/echo-trace")
        async def _echo():
            from src.core.trace import current_span_id, current_trace_id

            return {"trace_id": current_trace_id(), "span_id": current_span_id()}

        return app

    @pytest.fixture()
    def client(self, app: FastAPI) -> TestClient:
        return TestClient(app)

    def test_valid_traceparent_preserves_and_responds(self, client: TestClient):
        r = client.get(
            "/echo-trace",
            headers={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert len(body["span_id"]) == 16
        # traceresponse header should be present
        assert "traceresponse" in r.headers
        tr = r.headers["traceresponse"]
        assert "4bf92f3577b34da6a3ce929d0e0e4736" in tr

    def test_malformed_traceparent_starts_fresh(self, client: TestClient):
        r = client.get(
            "/echo-trace",
            headers={"traceparent": "garbage"},
        )
        assert r.status_code == 200
        body = r.json()
        # Fresh trace: different trace_id
        assert body["trace_id"] != ""
        assert len(body["trace_id"]) == 32

    def test_no_traceparent_generates_fresh(self, client: TestClient):
        r = client.get("/echo-trace")
        assert r.status_code == 200
        body = r.json()
        assert len(body["trace_id"]) == 32
        assert len(body["span_id"]) == 16


class TestNoopTracer:
    """NoopTracer does not fail."""

    def test_start_span(self):
        tracer = NoopTracer()
        span = tracer.start_span("test")
        span.set_attribute("key", "value")
        span.end()

    def test_context_manager(self):
        tracer = NoopTracer()
        with tracer.start_span("test") as span:
            span.set_attribute("key", "value")
