# SPDX-License-Identifier: FSL-1.1-ALv2
"""Core middleware: RequestId, AccessLog (design.md §4.3).

Middleware stack ordering (outermost first):
  1. ServerErrorMiddleware (Starlette built-in, outermost)
  2. RequestIdMiddleware
  3. TraceContextMiddleware
  4. AccessLogMiddleware
  5. CORSMiddleware
  6. (Phase 1) TenantContextMiddleware — insertion point documented here

Starlette add_middleware PREPENDS, so registration order in code is the REVERSE
of execution order. Register innermost-first to achieve the above stack.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging import request_id_var

logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assigns a unique request ID to every request via contextvar.

    Position 2 in the stack (after ServerError, before TraceContext).
    Every log line and problem body needs an id, including auth failures.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Use existing X-Request-ID header if provided, otherwise generate one
        req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = request_id_var.set(req_id)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = req_id
            return response
        finally:
            request_id_var.reset(token)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Logs request/response after IDs exist, before business logic.

    Position 4 in the stack (after TraceContext, before CORS).
    Records method, path, status code, and duration.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        logger.info(
            "%s %s %d %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 1),
            },
        )
        return response
