# SPDX-License-Identifier: FSL-1.1-ALv2
"""Task 7.3 — default-profile liveness/readiness outage semantics.

Appendix E criterion 5 has to hold for the service as it actually runs, not just
for an in-process ASGI harness, so this test starts a REAL uvicorn server (the
same command the backend image uses) and drives it over REAL HTTP:

  Phase A — both dependencies unreachable (ports with nothing listening):
      GET /health          -> 200   (liveness performs no dependency I/O)
      GET /api/v1/health   -> 200
      GET /health/ready    -> RFC 9457 503 naming postgres AND redis
  Phase B — recovery without restarting the process:
      a real PostgreSQL is already reachable and a real redis-server is started
      mid-flight, and readiness must flip to 200 on its own.

It also asserts the division of labour that design §4.4 and §13.3 require: the
Compose container healthcheck is LIVENESS only, while scripts/dev-up.sh is what
gates on READINESS.

Every subprocess call is bounded by a timeout and every server is terminated in
a finally block, so this test cannot hang or leak processes.

Run: python scripts/tests/health_outage_test.py
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
COMPOSE = ROOT / "docker-compose.yml"
DEV_UP = ROOT / "scripts" / "dev-up.sh"

VENV_PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"
VENV_PYTHON_POSIX = BACKEND / ".venv" / "bin" / "python"

# The already-running local test cluster (see docs/development.md); overridable.
REAL_DATABASE_URL = os.environ.get(
    "FORGEOPS_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:25432/forgeops_test",
)
REDIS_SERVER = os.environ.get(
    "FORGEOPS_TEST_REDIS_SERVER", r"C:\Program Files\Redis\redis-server.exe"
)

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"ok   - {msg}")


def bad(msg: str) -> None:
    print(f"FAIL - {msg}", file=sys.stderr)
    failures.append(msg)


def backend_python() -> str:
    for candidate in (VENV_PYTHON, VENV_PYTHON_POSIX):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # connection refused, timeout, ...
        return 0, str(exc)


def wait_for_http(url: str, want: int, timeout: float) -> tuple[int, str]:
    deadline = time.monotonic() + timeout
    status, body = 0, ""
    while time.monotonic() < deadline:
        status, body = http_get(url)
        if status == want:
            return status, body
        time.sleep(0.5)
    return status, body


class Uvicorn:
    """A real backend server process bound to an ephemeral port."""

    def __init__(self, *, database_url: str, redis_url: str) -> None:
        self.port = free_port()
        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": database_url,
                "REDIS_URL": redis_url,
                "APP_ENV": "development",
                "LOG_FORMAT": "json",
                "MCP_OIDC_AUDIENCE": env.get("MCP_OIDC_AUDIENCE", "forgeops-mcp-gateway"),
                "OPA_URL": env.get("OPA_URL", "http://127.0.0.1:8181"),
                "PYTHONPATH": str(BACKEND),
            }
        )
        self._env = env
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> "Uvicorn":
        self._proc = subprocess.Popen(
            [
                backend_python(),
                "-m",
                "uvicorn",
                "src.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=str(BACKEND),
            env=self._env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        status, _ = wait_for_http(self.url("/health"), 200, timeout=45)
        if status != 200:
            self.__exit__(None, None, None)
            raise RuntimeError("uvicorn did not start (liveness never answered 200)")
        return self

    def __exit__(self, *_exc) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._proc.kill()
            self._proc = None

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def case_outage() -> None:
    print("# phase A: both dependencies unreachable")
    closed_pg, closed_redis = free_port(), free_port()
    try:
        server = Uvicorn(
            database_url=f"postgresql+asyncpg://forgeops:pw@127.0.0.1:{closed_pg}/forgeops",
            redis_url=f"redis://127.0.0.1:{closed_redis}/0",
        )
    except RuntimeError as exc:
        bad(f"backend did not start during a dependency outage: {exc}")
        return

    with server:
        ok("the backend process starts and serves traffic with both dependencies down")
        for path in ("/health", "/api/v1/health"):
            status, body = http_get(server.url(path))
            if status == 200:
                ok(f"{path} stays 200 during the outage")
            else:
                bad(f"{path} returned {status} during the outage: {body[:200]}")

        status, body = http_get(server.url("/health/ready"))
        if status == 503:
            ok("/health/ready returns 503 during the outage")
        else:
            bad(f"/health/ready returned {status}, expected 503: {body[:200]}")
        try:
            problem = json.loads(body)
        except ValueError:
            bad("/health/ready body is not JSON")
            return
        if problem.get("status") == 503:
            ok("the readiness problem body status equals the HTTP status")
        else:
            bad(f"problem body status was {problem.get('status')!r}")
        if str(problem.get("type", "")).endswith("/not-ready"):
            ok("the readiness problem carries the stable not-ready type URI")
        else:
            bad(f"unexpected problem type: {problem.get('type')!r}")
        named = json.dumps(problem.get("errors", [])).lower()
        for dependency in ("postgres", "redis"):
            if dependency in named:
                ok(f"the readiness problem names the failed dependency: {dependency}")
            else:
                bad(f"the readiness problem does not name {dependency}: {named}")


def case_recovery() -> None:
    print("# phase B: recovery without restarting the process")
    if not os.path.exists(REDIS_SERVER):
        bad(f"no real redis-server at {REDIS_SERVER}; cannot prove recovery")
        return

    redis_port = free_port()
    redis_proc: subprocess.Popen[bytes] | None = None
    try:
        server = Uvicorn(
            database_url=REAL_DATABASE_URL,
            redis_url=f"redis://127.0.0.1:{redis_port}/0",
        )
    except RuntimeError as exc:
        bad(f"backend did not start for the recovery phase: {exc}")
        return

    try:
        with server:
            status, body = http_get(server.url("/health/ready"))
            if status == 503 and "redis" in body.lower():
                ok("/health/ready reports 503 naming redis while it is down")
            else:
                bad(f"expected a redis-specific 503, got {status}: {body[:200]}")

            redis_proc = subprocess.Popen(
                [
                    REDIS_SERVER,
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

            status, body = wait_for_http(server.url("/health/ready"), 200, timeout=45)
            if status == 200:
                ok("/health/ready recovers to 200 in the SAME process once Redis returns")
                payload = json.loads(body)
                if payload.get("checks") == {"postgres": "ok", "redis": "ok"}:
                    ok("both dependency checks report ok after recovery")
                else:
                    bad(f"unexpected readiness checks payload: {payload}")
            else:
                bad(f"readiness did not recover; last status {status}: {body[:200]}")

            live, _ = http_get(server.url("/health"))
            if live == 200:
                ok("/health remained 200 throughout the outage and recovery")
            else:
                bad(f"/health returned {live} after recovery")
    finally:
        if redis_proc is not None:
            redis_proc.terminate()
            try:
                redis_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                redis_proc.kill()


def case_probe_division_of_labour() -> None:
    print("# liveness gates the container, readiness gates dev-up")
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    probe = " ".join(
        str(x) for x in data["services"]["backend"]["healthcheck"]["test"]
    )
    if "/health" in probe and "/health/ready" not in probe:
        ok("the Compose backend healthcheck probes /health only (liveness)")
    else:
        bad(f"the backend container healthcheck must be liveness only: {probe!r}")

    dev_up = DEV_UP.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in dev_up.splitlines() if not line.lstrip().startswith("#")
    )
    if "/health/ready" in code:
        ok("scripts/dev-up.sh gates on /health/ready (readiness)")
    else:
        bad("scripts/dev-up.sh must gate on /health/ready")


def main() -> int:
    case_outage()
    case_recovery()
    case_probe_division_of_labour()
    print()
    if failures:
        print(
            f"health outage test FAILED ({len(failures)} failing assertion(s))",
            file=sys.stderr,
        )
        return 1
    print("health outage test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
