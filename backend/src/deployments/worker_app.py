# SPDX-License-Identifier: FSL-1.1-ALv2
"""Worker ASGI Application and Internal Client Provider.

Provides a thread-safe, concurrency-safe singleton FastAPI app instance for the ARQ
worker execution context. Manages application lifespan and internal request authorization
via ASGITransport without global dependency overrides or shared session conflicts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import FastAPI

from ..auth.dependencies import PRINCIPAL_STATE_ATTR
from ..auth.principal import Principal
from ..core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_worker_app: FastAPI | None = None
_worker_lifespan_cm: Any = None
_worker_lock = asyncio.Lock()


async def get_or_create_worker_app(settings: Settings | None = None) -> FastAPI:
    """Lazily constructs and initializes the FastAPI application within the worker process.

    Enters the application lifespan exactly once so that state resources (such as
    governance chokepoint, model router, and semantic cache) are fully initialized.
    """
    global _worker_app, _worker_lifespan_cm

    if _worker_app is not None:
        return _worker_app

    async with _worker_lock:
        if _worker_app is not None:
            return _worker_app

        effective_settings = settings or get_settings()
        from src.main import create_app, lifespan

        app = create_app(settings=effective_settings)
        cm = lifespan(app)
        await cm.__aenter__()
        _worker_lifespan_cm = cm
        _worker_app = app
        logger.info("Worker ASGI application initialized with active lifespan")
        return _worker_app


async def shutdown_worker_app() -> None:
    """Closes the worker ASGI application lifespan cleanly."""
    global _worker_app, _worker_lifespan_cm

    async with _worker_lock:
        if _worker_lifespan_cm is not None:
            try:
                await _worker_lifespan_cm.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("Error during worker application lifespan shutdown: %s", exc)
            _worker_lifespan_cm = None
        _worker_app = None


def create_worker_asgi_client(
    app: FastAPI,
    principal: Principal,
    timeout_seconds: float = 300.0,
) -> httpx.AsyncClient:
    """Creates an httpx.AsyncClient using ASGITransport with request-scoped principal injection.

    Injects the specified Principal directly into the ASGI scope state for each request,
    satisfying require_principal and TenantContextMiddleware without global dependency
    overrides or concurrency crosstalk.
    """

    async def auth_asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            if "state" not in scope:
                scope["state"] = {}
            scope["state"][PRINCIPAL_STATE_ATTR] = principal
        await app(scope, receive, send)

    transport = httpx.ASGITransport(app=auth_asgi_app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=httpx.Timeout(timeout_seconds, connect=10.0),
    )
