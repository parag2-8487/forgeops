# SPDX-License-Identifier: FSL-1.1-ALv2
"""A real loopback HTTP server answering OPA's health path (task 9.2, §0.4.1).

The same shape, and the same argument, as `cerbos_stub.py`. Leaf 9.2 adds OPA to
`/health/ready`, which changes the answer for two tests whose subject is something else:
`test_lifespan_health.py` observes Redis going from unavailable to available in one process,
and `test_readiness_excludes_idp.py` observes that an IdP outage does not affect readiness.
Making either require a real OPA container would widen its capability surface for a
dependency it is not about, and a test that needs four services to prove one thing is a test
that gets skipped.

A **transport** is substituted, which §0.4.1 permits, and nothing else: the readiness handler
builds its own request and speaks over a real TCP socket to a real HTTP server. There is no
`Mock`, so `FO-TD004` has nothing to object to.

`test_governance_policy_opa.py` is where the wire format and every decision are proved,
against the digest-pinned `openpolicyagent/opa:1.4.2` under `require_capability("opa")`. This
stub answers only `/health` and deliberately 404s `/v1/data/**`, so a test that accidentally
evaluated a policy against it fails loudly rather than receiving a fabricated allow — which
would be the worst possible outcome for a governance test.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/health":
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        """Deliberately refuses to answer a policy question.

        A stub that returned a decision would be a governance bundle nobody reviewed, and the
        first test to lean on it would be asserting this file's opinion rather than the
        bundle's. 404 makes the client raise `PolicySourceUnavailableError`, which the
        chokepoint turns into a deny — the fail-closed direction.
        """
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence the default stderr access log; pytest output is signal, not noise."""


@contextmanager
def opa_health_stub() -> Iterator[str]:
    """Yield the base URL of a running stub, shut down on exit."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


__all__ = ["opa_health_stub"]
