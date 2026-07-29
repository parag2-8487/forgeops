# SPDX-License-Identifier: FSL-1.1-ALv2
"""Behavioural test for scripts/dev-up.sh readiness polling (task 7.1).

design.md §13.3 requires `make up` to start the unprofiled default profile and
then poll `/health/ready`, reporting the failing dependencies by name when it
gives up. Those three paths — success, timeout, and named failure — are really
executed here against an in-process stand-in that serves the exact §4.4
readiness contract.

Why this harness is Python rather than shell: the stand-in has to run WHILE
dev-up.sh polls it. Managing a background server from a POSIX script under MSYS
leaves orphaned processes that hold the parent's pipes open and hang the caller.
Here the server runs on a daemon thread inside this process and every dev-up.sh
invocation goes through subprocess.run(..., timeout=...), so the test can never
hang: it fails loudly instead.

The `docker compose up` step needs a container engine, so it is skipped through
FORGEOPS_SKIP_COMPOSE; the assertion that dev-up.sh passes no --profile flag is
covered by scripts/tests/dev-up.test.sh.

Run: python scripts/tests/dev_up_readiness_test.py
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEV_UP = ROOT / "scripts" / "dev-up.sh"

READY_BODY = {"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}}
UNREADY_BODY = {
    "type": "https://errors.forgeops.dev/not-ready",
    "title": "Service not ready",
    "status": 503,
    "detail": "One or more dependencies are unavailable.",
    "instance": "/health/ready",
    "errors": [
        {"dependency": "postgres", "detail": "health check timed out"},
        {"dependency": "redis", "detail": "connection refused"},
    ],
}


def resolve_bash() -> str:
    """Return a POSIX bash that can actually run the project scripts.

    On Windows the first `bash` on PATH is frequently the WSL shim
    (C:\\Windows\\System32\\bash.exe). With no distribution installed it fails with
    "execvpe(/bin/bash) failed", which looks like a script bug but is not one, so
    Git Bash is preferred explicitly and only then does PATH lookup apply.
    """
    candidates = [
        os.environ.get("FORGEOPS_BASH", ""),
        r"C:\\Program Files\\Git\\bin\\bash.exe",
        r"C:\\Program Files (x86)\\Git\\bin\\bash.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    found = shutil.which("bash")
    if found and "System32" not in found:
        return found
    raise RuntimeError("no POSIX bash found; set FORGEOPS_BASH to a bash executable")

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"ok   - {msg}")


def bad(msg: str) -> None:
    print(f"FAIL - {msg}", file=sys.stderr)
    failures.append(msg)


def _handler_for(ready: bool):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/health/ready":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(READY_BODY if ready else UNREADY_BODY).encode()
            self.send_response(200 if ready else 503)
            self.send_header(
                "Content-Type",
                "application/json" if ready else "application/problem+json",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler


class StandIn:
    """Readiness stand-in bound to an ephemeral port, on a daemon thread."""

    def __init__(self, ready: bool) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(ready))
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "StandIn":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def run_dev_up(port: int | None, *, timeout_s: int, interval_s: int = 1):
    """Invoke dev-up.sh with a hard wall-clock bound."""
    bash = resolve_bash()
    env = dict(os.environ)
    env["FORGEOPS_SKIP_COMPOSE"] = "1"
    env["FORGEOPS_READY_TIMEOUT"] = str(timeout_s)
    env["FORGEOPS_READY_INTERVAL"] = str(interval_s)
    if port is not None:
        env["FORGEOPS_READY_URL"] = f"http://127.0.0.1:{port}/health/ready"
    return subprocess.run(
        [bash, str(DEV_UP)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        # Generous relative to the script's own bound, but finite: a hang is a
        # test failure, never an indefinite wait.
        timeout=timeout_s + 60,
    )


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def case_ready_succeeds() -> None:
    print("# case 1: readiness answers 200 -> dev-up exits 0")
    with StandIn(ready=True) as srv:
        try:
            r = run_dev_up(srv.port, timeout_s=15)
        except subprocess.TimeoutExpired:
            bad("dev-up.sh hung against a ready backend")
            return
    if r.returncode == 0:
        ok("exits 0 once /health/ready returns 200")
    else:
        bad(f"exited {r.returncode} against a ready backend: {r.stderr.strip()}")
    if "reports ready" in r.stdout:
        ok("reports that the backend became ready")
    else:
        bad(f"success output did not confirm readiness: {r.stdout.strip()!r}")


def case_unready_times_out_and_names_dependencies() -> None:
    print("# case 2: readiness stays 503 -> times out and NAMES each dependency")
    with StandIn(ready=False) as srv:
        try:
            r = run_dev_up(srv.port, timeout_s=4)
        except subprocess.TimeoutExpired:
            bad("dev-up.sh did not honour its own readiness timeout")
            return
    if r.returncode != 0:
        ok("exits non-zero when readiness never succeeds")
    else:
        bad("exited 0 even though readiness never returned 200")
    combined = (r.stdout + r.stderr).lower()
    if "did not become ready" in combined:
        ok("reports the readiness timeout")
    else:
        bad("timeout message missing")
    for dep in ("postgres", "redis"):
        if dep in combined:
            ok(f"names the unready dependency: {dep}")
        else:
            bad(f"did not name the unready dependency: {dep}")
    if "unready dependencies:" in combined:
        ok("prints an explicit unready-dependency summary line")
    else:
        bad("no explicit unready-dependency summary line")


def case_nothing_listening() -> None:
    print("# case 3: nothing listening -> bounded failure, no hang")
    port = free_port()
    try:
        r = run_dev_up(port, timeout_s=3)
    except subprocess.TimeoutExpired:
        bad("dev-up.sh hung when nothing was listening")
        return
    if r.returncode != 0:
        ok("exits non-zero within the bounded timeout when nothing is listening")
    else:
        bad("exited 0 with nothing listening on the readiness port")
    combined = r.stdout + r.stderr
    if "no readiness response body" in combined or "did not become ready" in combined:
        ok("explains that no readiness response was received")
    else:
        bad(f"unhelpful failure output: {combined.strip()!r}")


def main() -> int:
    if not DEV_UP.is_file():
        print(f"FAIL - {DEV_UP} is missing", file=sys.stderr)
        return 1
    case_ready_succeeds()
    case_unready_times_out_and_names_dependencies()
    case_nothing_listening()
    print()
    if failures:
        print(
            f"dev-up readiness test FAILED ({len(failures)} failing assertion(s))",
            file=sys.stderr,
        )
        return 1
    print("dev-up readiness test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
