# SPDX-License-Identifier: FSL-1.1-ALv2
"""RFC 9457 Problem Details error rendering (design.md §4.2, §11.2).

Every non-2xx response carries application/problem+json with status matching HTTP
status, instance = request path, trace_id, and detail that never leaks secrets.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from .logging import trace_id_var

PROBLEM_CONTENT_TYPE = "application/problem+json"
TYPE_BASE = "https://errors.forgeops.dev"

logger = logging.getLogger(__name__)

# Patterns to scrub from error details before they reach clients
_LEAK_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"postgresql(?:\+\w+)?://[^\s]+"),
    re.compile(r"redis://[^\s]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9]{20,}"),
    re.compile(r"xai-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?(?:PRIVATE|PUBLIC)\s+KEY-----"),
    re.compile(r"Traceback \(most recent call last\)"),
]


def _sanitize_detail(detail: str | None) -> str | None:
    """Remove secrets and tracebacks from detail text before it reaches clients."""
    if detail is None:
        return None
    for pattern in _LEAK_PATTERNS:
        if pattern.search(detail):
            return None  # If any leak pattern matches, suppress the detail entirely
    return detail


def _slugify(text: str) -> str:
    """Convert a phrase to a URL-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class ProblemDetail(BaseModel):
    """RFC 9457 Problem Details response body."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    trace_id: str | None = None
    errors: list[dict[str, str]] | None = None


class ProblemException(Exception):
    """Raise to produce an RFC 9457 response with the given problem shape."""

    def __init__(
        self,
        *,
        status: int,
        type_suffix: str,
        title: str,
        detail: str | None = None,
        errors: list[dict[str, str]] | None = None,
    ):
        self.problem = ProblemDetail(
            type=f"{TYPE_BASE}/{type_suffix}",
            title=title,
            status=status,
            detail=detail,
            errors=errors,
        )
        super().__init__(title)


def _render(request: Request, problem: ProblemDetail) -> Response:
    """Render a ProblemDetail as an application/problem+json response."""
    problem.instance = request.url.path
    problem.trace_id = trace_id_var.get("") or None
    # Ensure detail is sanitized
    problem.detail = _sanitize_detail(problem.detail)
    return Response(
        content=problem.model_dump_json(exclude_none=True),
        status_code=problem.status,
        media_type=PROBLEM_CONTENT_TYPE,
    )


def install_problem_handlers(app: FastAPI) -> None:
    """Install RFC 9457 exception handlers for all error types."""

    @app.exception_handler(ProblemException)
    async def _problem(request: Request, exc: ProblemException) -> Response:
        return _render(request, exc.problem)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        errors_list = []
        for e in exc.errors():
            loc_parts = [str(p) for p in e["loc"][1:]] if len(e["loc"]) > 1 else [str(p) for p in e["loc"]]
            pointer = "#/" + "/".join(loc_parts) if loc_parts else "#/"
            errors_list.append({"pointer": pointer, "detail": e["msg"]})
        return _render(
            request,
            ProblemDetail(
                type=f"{TYPE_BASE}/validation-failed",
                title="Request validation failed",
                status=422,
                detail="One or more fields failed validation.",
                errors=errors_list,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        status_code = exc.status_code
        try:
            phrase = HTTPStatus(status_code).phrase
        except ValueError:
            phrase = "Unknown Error"
        detail_text = exc.detail if isinstance(exc.detail, str) else None
        return _render(
            request,
            ProblemDetail(
                type=f"{TYPE_BASE}/{_slugify(phrase)}",
                title=phrase,
                status=status_code,
                detail=_sanitize_detail(detail_text),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        logger.exception("unhandled exception")
        return _render(
            request,
            ProblemDetail(
                type=f"{TYPE_BASE}/internal",
                title="Internal Server Error",
                status=500,
                detail="An unexpected error occurred. Quote the trace_id when reporting this.",
            ),
        )
