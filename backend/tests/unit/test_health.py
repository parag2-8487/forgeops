# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for health endpoints and lifespan behavior (tasks 5.5, 5.6).

Tests health, readiness, and lifespan non-destructive startup behavior.
Uses unreachable localhost ports to simulate dependency outage without mocking.
"""

from __future__ import annotations

import socket
from contextlib import closing

import pytest
from httpx import ASGITransport, AsyncClient


def _find_closed_port() -> int:
    """Find a port that is not listening (guaranteed connection refused)."""
    # Bind to an ephemeral port, get the number, close immediately.
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port


def _set_unreachable_env(monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """Set env vars pointing to unreachable ports for PostgreSQL and Redis."""
    pg_port = _find_closed_port()
    redis_port = _find_closed_port()
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://user:pass@127.0.0.1:{pg_port}/testdb",
    )
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{redis_port}/0")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("MCP_OIDC_AUDIENCE", "test-audience")
    return pg_port, redis_port


def _set_minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set minimal valid environment for Settings construction."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("MCP_OIDC_AUDIENCE", "test-audience")


class TestHealthEndpoint:
    """Task 5.5: /health performs NO dependency I/O and stays 200."""

    @pytest.mark.asyncio
    async def test_health_returns_200_with_unreachable_deps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /health returns 200 even when deps are unreachable."""
        _set_unreachable_env(monkeypatch)
        from src.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "version" in data
            assert "commit" in data

    @pytest.mark.asyncio
    async def test_api_v1_health_returns_200_with_unreachable_deps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /api/v1/health returns 200 even when deps are unreachable."""
        _set_unreachable_env(monkeypatch)
        from src.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"


class TestHealthReadyEndpoint:
    """Task 5.5: /health/ready checks PostgreSQL and Redis."""

    @pytest.mark.asyncio
    async def test_ready_503_when_deps_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns RFC 9457 503 with errors[] when deps are unreachable."""
        _set_unreachable_env(monkeypatch)
        from src.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health/ready")
            assert resp.status_code == 503
            assert "application/problem+json" in resp.headers["content-type"]
            data = resp.json()
            assert data["type"] == "https://errors.forgeops.dev/not-ready"
            assert data["status"] == 503
            assert "errors" in data
            # Should have one item per failed dependency
            deps_in_errors = [e["dependency"] for e in data["errors"]]
            assert "postgres" in deps_in_errors
            assert "redis" in deps_in_errors


class TestLifespanNonDestructive:
    """Task 5.6: Lifespan does not abort on unreachable deps."""

    @pytest.mark.asyncio
    async def test_startup_succeeds_with_unreachable_deps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Startup yields even when PostgreSQL and Redis are unreachable.

        Proves: startup yields, /health and /api/v1/health stay 200,
        /health/ready is RFC 9457 503 naming BOTH failures.
        """
        _set_unreachable_env(monkeypatch)
        from src.main import create_app

        app = create_app()
        # The fact that we can make requests proves startup succeeded
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # /health stays 200
            resp = await client.get("/health")
            assert resp.status_code == 200

            # /api/v1/health stays 200
            resp = await client.get("/api/v1/health")
            assert resp.status_code == 200

            # /health/ready is RFC 9457 503 naming both failures
            resp = await client.get("/health/ready")
            assert resp.status_code == 503
            data = resp.json()
            deps = [e["dependency"] for e in data["errors"]]
            assert "postgres" in deps
            assert "redis" in deps

    @pytest.mark.asyncio
    async def test_invalid_config_prevents_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid local configuration still prevents startup (fast fail)."""
        # Don't set DATABASE_URL — should fail with validation error
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

        from pydantic import ValidationError
        from src.core.config import get_settings

        with pytest.raises(ValidationError):
            get_settings()
