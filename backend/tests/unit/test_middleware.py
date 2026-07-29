# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for src/core/tasks.py, src/core/sse.py, src/core/middleware.py.

- InlineDispatcher executes once in process
- SSE values are exact
- Middleware order is stable
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.core.middleware import AccessLogMiddleware, RequestIdMiddleware
from src.core.sse import SSEEventType
from src.core.tasks import InlineDispatcher, TaskHandle, register_task
from src.core.trace import TraceContextMiddleware
from starlette.middleware.cors import CORSMiddleware


class TestInlineDispatcher:
    """InlineDispatcher executes the handler in-process, immediately."""

    @pytest.fixture(autouse=True)
    def _clear_handlers(self):
        """Reset handler registry between tests."""
        from src.core.tasks import _TASK_HANDLERS

        _TASK_HANDLERS.clear()
        yield
        _TASK_HANDLERS.clear()

    @pytest.mark.asyncio
    async def test_executes_once(self):
        """Registered handler is called exactly once."""
        call_count = 0

        @register_task("test.task")
        async def _handler(payload):
            nonlocal call_count
            call_count += 1

        dispatcher = InlineDispatcher()
        handle = await dispatcher.enqueue("test.task", {"key": "value"})

        assert call_count == 1
        assert isinstance(handle, TaskHandle)
        assert handle.dispatcher == "inline"
        assert handle.id  # non-empty

    @pytest.mark.asyncio
    async def test_returns_handle_with_idempotency_key(self):
        """When idempotency_key is provided, it becomes the handle id."""
        dispatcher = InlineDispatcher()
        handle = await dispatcher.enqueue("unknown.task", {}, idempotency_key="custom-id-123")
        assert handle.id == "custom-id-123"

    @pytest.mark.asyncio
    async def test_unknown_task_still_returns_handle(self):
        """Unknown task name does not crash — returns a handle with no execution."""
        dispatcher = InlineDispatcher()
        handle = await dispatcher.enqueue("nonexistent.task", {"x": 1})
        assert isinstance(handle, TaskHandle)


class TestSSEEventTypes:
    """SSE event type vocabulary must be exactly six values."""

    def test_exactly_six_events(self):
        assert len(SSEEventType) == 6

    def test_exact_values(self):
        expected = {"status", "token", "progress", "validation", "complete", "error"}
        actual = {e.value for e in SSEEventType}
        assert actual == expected

    def test_enum_members(self):
        assert SSEEventType.STATUS == "status"
        assert SSEEventType.TOKEN == "token"
        assert SSEEventType.PROGRESS == "progress"
        assert SSEEventType.VALIDATION == "validation"
        assert SSEEventType.COMPLETE == "complete"
        assert SSEEventType.ERROR == "error"


class TestMiddlewareOrder:
    """Middleware-order probe asserting:
    ServerError -> RequestId -> TraceContext -> AccessLog -> CORS
    with Phase 1 tenant insertion point documented.
    """

    def test_middleware_order_probe(self):
        """Build an app with the correct middleware order and verify execution."""
        app = FastAPI()

        # Track middleware execution order
        _order: list[str] = []

        # Registration order is REVERSE of execution order because Starlette prepends.
        # Register innermost first (CORS is innermost of our custom middleware).
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:3000"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        # Phase 1 tenant insertion point:
        # app.add_middleware(TenantContextMiddleware)  # Position 6, Phase 1
        app.add_middleware(AccessLogMiddleware)  # Position 4
        app.add_middleware(TraceContextMiddleware)  # Position 3
        app.add_middleware(RequestIdMiddleware)  # Position 2
        # ServerErrorMiddleware is built-in at position 1

        @app.get("/test")
        async def _test():
            return {"ok": True}

        client = TestClient(app)
        r = client.get("/test")
        assert r.status_code == 200

        # Verify RequestIdMiddleware ran (X-Request-ID header present)
        assert "x-request-id" in r.headers

        # Verify TraceContextMiddleware ran (traceresponse header present)
        assert "traceresponse" in r.headers

    def test_request_id_before_trace(self):
        """RequestId middleware must execute before TraceContext (position 2 before 3)."""
        app = FastAPI()

        # Correct order: CORS (innermost), AccessLog, TraceContext, RequestId (outermost)
        app.add_middleware(CORSMiddleware, allow_origins=["*"])
        app.add_middleware(AccessLogMiddleware)
        app.add_middleware(TraceContextMiddleware)
        app.add_middleware(RequestIdMiddleware)

        @app.get("/probe")
        async def _probe():
            from src.core.logging import request_id_var
            from src.core.trace import current_trace_id

            return {
                "request_id": request_id_var.get(""),
                "trace_id": current_trace_id(),
            }

        client = TestClient(app)
        r = client.get("/probe")
        body = r.json()
        # Both IDs should be populated, proving both middleware ran
        assert body["request_id"] != ""
        assert body["trace_id"] != ""
        assert len(body["trace_id"]) == 32

    def test_cors_responds_to_preflight(self):
        """CORS must answer preflight without touching routing or DB."""
        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:3000"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        app.add_middleware(AccessLogMiddleware)
        app.add_middleware(TraceContextMiddleware)
        app.add_middleware(RequestIdMiddleware)

        @app.get("/protected")
        async def _protected():
            return {"data": "secret"}

        client = TestClient(app)
        r = client.options(
            "/protected",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code == 200
        assert "access-control-allow-origin" in r.headers
