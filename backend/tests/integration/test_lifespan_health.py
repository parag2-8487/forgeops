# SPDX-License-Identifier: FSL-1.1-ALv2
"""Task 5.6 — lifespan and health during dependency loss and recovery.

design.md §4.4 and §11.1 make three promises that only an integration test can
prove, because they are all about what happens when the dependencies are NOT
there:

  1. Startup does not abort merely because PostgreSQL and Redis are unreachable.
     The lifespan constructs engines/clients non-destructively and only logs
     warnings from its best-effort probes.
  2. `/health` (and `/api/v1/health`) stay 200 through the outage, because
     liveness performs no dependency I/O at all.
  3. `/health/ready` returns an RFC 9457 503 naming EVERY failed or timed-out
     dependency, and recovers to 200 once they come back WITHOUT the process
     being restarted.

Plus the inverse guarantee: invalid local configuration must still fail fast.

The outage is created by pointing the app at a TCP port with nothing listening.
Recovery is then genuinely demonstrated by starting real servers on those exact
ports — a real PostgreSQL 17 cluster and a real `redis-server` — rather than by
patching the probes, so the connection-retry behaviour (`pool_pre_ping`, lazy
Redis client) is what is actually under test.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from .capability import require_capability
from .cerbos_stub import cerbos_health_stub

PROBLEM_CONTENT_TYPE = "application/problem+json"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_is_closed(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


@pytest.fixture(autouse=True)
def _restore_environment() -> Iterator[None]:
    """Undo this module's environment mutations after every test.

    `_build_app` assigns `DATABASE_URL` and `REDIS_URL` directly, because the app
    factory reads them through pydantic-settings at construction time. Those
    assignments used to persist, so every later test in the session saw a DSN
    pointing at a deliberately-closed port — it surfaced in CI as
    `test_cas_holds_against_a_real_redis` failing to reach Redis on a random high
    port. A process-wide mutation leaking out of a test is a defect even when the
    ordering happens to hide it.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
        _clear_settings_cache()


def _clear_settings_cache() -> None:
    from src.core.config import get_settings

    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()


def _build_app(database_url: str, redis_url: str):
    """Import the app factory with a project configuration applied to the env."""
    # Since debt D1 was closed (task 2.1) the lifespan builds the model router from
    # config/model-tiers.yaml, whose `base_url` values are `${VAR}` placeholders that
    # load_tier_config refuses to leave unexpanded. The committed baseline supplies
    # them, which keeps this test a CONFIGURATION substitution and simultaneously
    # asserts that `.env.example` is complete enough to boot the app.
    from src.core.config import load_project_dotenv

    for key, value in load_project_dotenv((".env.example",)).items():
        os.environ.setdefault(key, value)

    os.environ["DATABASE_URL"] = database_url
    os.environ["REDIS_URL"] = redis_url
    os.environ.setdefault("MCP_OIDC_AUDIENCE", "forgeops-mcp-gateway")
    os.environ.setdefault("OPA_URL", "http://127.0.0.1:8181")
    from src.main import create_app

    _clear_settings_cache()
    return create_app()


@pytest.fixture()
def outage_ports() -> Iterator[tuple[int, int]]:
    """Two ports with nothing listening on them."""
    pg_port, redis_port = _free_port(), _free_port()
    assert _port_is_closed(pg_port) and _port_is_closed(redis_port)
    yield pg_port, redis_port


class TestLifespanDuringOutage:
    def test_startup_succeeds_and_liveness_stays_200_while_readiness_503(self, outage_ports: tuple[int, int]) -> None:
        pg_port, redis_port = outage_ports
        app = _build_app(
            f"postgresql+asyncpg://forgeops:pw@127.0.0.1:{pg_port}/forgeops",
            f"redis://127.0.0.1:{redis_port}/0",
        )

        # Entering the TestClient context runs the real ASGI lifespan. If the
        # lifespan aborted on an unreachable dependency this would raise.
        with TestClient(app) as client:
            for path in ("/health", "/api/v1/health"):
                live = client.get(path)
                assert live.status_code == 200, (
                    f"{path} must stay 200 during a dependency outage: {live.status_code} {live.text}"
                )
                assert live.json()["status"] == "ok"

            ready = client.get("/health/ready")
            assert ready.status_code == 503
            assert ready.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
            body = ready.json()
            assert body["status"] == 503, "RFC 9457 body status must equal the HTTP status"
            assert body["type"].endswith("/not-ready")
            named = " ".join(str(e) for e in body["errors"]).lower()
            assert "postgres" in named, body
            assert "redis" in named, body

    def test_invalid_configuration_still_prevents_startup(self) -> None:
        """The non-destructive lifespan must not swallow real config errors."""
        from pydantic import ValidationError
        from src.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                database_url="not-a-postgres-dsn",
                redis_url="redis://127.0.0.1:6379/0",
                mcp_oidc_audience="forgeops-mcp-gateway",
                opa_url="http://127.0.0.1:8181",
            )


class TestReadinessRecovery:
    """Readiness must recover in the SAME process once dependencies return."""

    def test_readiness_recovers_without_restarting_the_process(self, database_url: str, tmp_path) -> None:
        _pg_bin = os.environ.get("FORGEOPS_TEST_PG_BIN", r"C:\IMP\kiro\_toolchain\pg17\bin")
        # Prefer a redis-server on PATH (how Linux and CI have it) and fall back to
        # the local Windows install path. The previous default was Windows-only, so
        # this test skipped everywhere else — including CI.
        redis_server = os.environ.get("FORGEOPS_TEST_REDIS_SERVER", "") or (
            shutil.which("redis-server") or r"C:\Program Files\Redis\redis-server.exe"
        )
        if not os.path.exists(redis_server) and shutil.which(redis_server) is None:
            require_capability(
                "redis",
                f"no real redis-server binary available at {redis_server}; this test must "
                "start and stop its own Redis to observe the unavailable -> available transition",
            )

        # The already-running test PostgreSQL is reachable; Redis is started on a
        # port that is closed when the app boots, so the app must observe the
        # transition from unavailable to available on its own.
        redis_port = _free_port()
        assert _port_is_closed(redis_port)

        # Task 6.4 added Cerbos to readiness, so this test needs it reachable or the
        # 200 it waits for could never arrive — and the subject here is Redis
        # recovering, not authorisation. A transport substitution (§0.4.1): the
        # production client over a real socket, with a stub answering only health.
        with cerbos_health_stub() as cerbos_url:
            os.environ["CERBOS_URL"] = cerbos_url
            app = _build_app(database_url, f"redis://127.0.0.1:{redis_port}/0")
            self._observe_recovery(app, redis_server, redis_port)

    def _observe_recovery(self, app, redis_server: str, redis_port: int) -> None:
        redis_proc: subprocess.Popen[bytes] | None = None
        try:
            with TestClient(app) as client:
                first = client.get("/health/ready")
                assert first.status_code == 503, f"readiness must report 503 while Redis is down: {first.text}"
                assert "redis" in first.text.lower()
                assert client.get("/health").status_code == 200

                # Bring the missing dependency up. No restart, no re-import.
                redis_proc = subprocess.Popen(
                    [
                        redis_server,
                        "--port",
                        str(redis_port),
                        "--bind",
                        "127.0.0.1",
                        "--save",
                        "",
                        "--appendonly",
                        "no",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                assert _wait_for_port(redis_port), "redis-server did not start"

                deadline = time.monotonic() + 20.0
                recovered = None
                while time.monotonic() < deadline:
                    recovered = client.get("/health/ready")
                    if recovered.status_code == 200:
                        break
                    time.sleep(0.5)

                assert recovered is not None and recovered.status_code == 200, (
                    "readiness must recover in the same process once the "
                    f"dependency returns; last response: {recovered.text if recovered else None}"
                )
                payload = recovered.json()
                assert payload["status"] == "ready"
                # Still an EXACT set, now with the dependency task 6.4 added. Exact
                # rather than a subset check because a probe silently disappearing from
                # readiness is the failure this line exists to catch.
                assert payload["checks"] == {"postgres": "ok", "redis": "ok", "cerbos": "ok"}
        finally:
            if redis_proc is not None:
                redis_proc.terminate()
                try:
                    redis_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    redis_proc.kill()
