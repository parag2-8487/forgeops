# SPDX-License-Identifier: FSL-1.1-ALv2
"""A real loopback HTTP server answering Cerbos's health path (task 6.4, §0.4.1).

Why this exists
---------------
Task 6.4 adds Cerbos to `/health/ready`, which changes the answer for two tests whose
subject is something else entirely: `test_lifespan_health.py` observes Redis going from
unavailable to available in one process, and `test_readiness_excludes_idp.py` observes
that an IdP outage does not affect readiness. Making either of them require a real Cerbos
container would widen its capability surface for a dependency it is not about, and a test
that needs three services to prove one thing is a test that gets skipped.

What is substituted, and what is not
------------------------------------
A **transport**, which §0.4.1 permits, and nothing else. The production `CerbosClient`
runs unmodified, builds its own request and speaks over a real TCP socket to a real HTTP
server; only the process on the other end is not Cerbos. No collaborator is replaced, so
`FO-TD004` has nothing to object to — there is no `Mock` here.

`test_cerbos_matrix.py` is where the wire format is proved, against the digest-pinned
`ghcr.io/cerbos/cerbos:0.54.0` under `require_capability("cerbos")`. This stub answers
only the health path and deliberately 404s the check path, so a test that accidentally
tried to authorise something against it fails loudly instead of getting a fabricated
allow.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.auth.cerbos import HEALTH_PATH


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == HEALTH_PATH:
            body = b'{"status":"SERVING"}'
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
        """Deliberately refuses to answer an authorisation question.

        A stub that returned a decision would be a policy engine nobody reviewed, and
        the first test to lean on it would be asserting this file's opinion rather than
        the policy set's.
        """
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence the default stderr access log; pytest output is signal, not noise."""


@contextmanager
def cerbos_health_stub() -> Iterator[str]:
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


__all__ = ["cerbos_health_stub"]
