# SPDX-License-Identifier: FSL-1.1-ALv2
"""End-to-end wiring of the MCP gateway with its REAL collaborators.

Why this file exists
--------------------
Every other MCP test composed `McpGateway` with `AsyncMock` doubles. The doubles
implemented the contract the gateway wanted; the real `OpaGatewayPolicy`,
`TtlToolCache`, `McpUpstream` and `RedisTaskStore` implemented a different one. The
production composition in `src/main.py` therefore raised `TypeError` on every
`tools/list`, `tools/call` and `tasks/create` while 419 tests stayed green.

This test builds the graph the way `src/main.py` builds it — the real classes, in
the same constructor shape — and drives the requests through the real
`src/mcp/routes.py` handler with a `TestClient`. Only two things are substituted,
and neither is a collaborator of the gateway:

* the network, via `httpx.MockTransport`, so the real `McpUpstream` and
  `OpaGatewayPolicy` still build their requests and parse their responses;
* Redis, via an in-memory double that implements `SET PX` / `GET` / `PTTL` /
  `EVAL` faithfully, including the compare-and-set script's semantics.

Reintroducing any of the five original contract mismatches makes these tests fail.
Design authority: §11.4, §11.5.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.core.errors import install_problem_handlers
from src.mcp.auth import OidcTokenVerifier
from src.mcp.cache import TtlToolCache
from src.mcp.gateway import McpGateway
from src.mcp.policy import DEFAULT_ALLOW_PATH, DEFAULT_FILTER_PATH, OpaGatewayPolicy
from src.mcp.registry import McpServerRegistry
from src.mcp.routes import router as mcp_router
from src.mcp.routing import HeaderRouter
from src.mcp.tasks import RedisTaskStore
from src.mcp.upstream import McpUpstream

from .wiring import wires

# The issuer is supplied per test run by the jwks_server fixture, which serves a
# real JWKS document over loopback. Only the key id stays constant, because the
# token header and the served JWK have to agree on it.
KEY_ID = "wiring-key"
AUDIENCE = "forgeops-mcp-gateway"
UPSTREAM_URL = "http://agent.test:8900"

# The upstream advertises one read-only tool and one infrastructure tool, so the
# blast-radius filter has something real to remove.
UPSTREAM_TOOLS = [
    {"name": "agent.health", "description": "Agent liveness", "annotations": {"blast_radius": "read_only"}},
    {"name": "agent.tofu.apply", "description": "Apply", "annotations": {"blast_radius": "infrastructure"}},
]


# ── In-memory Redis that behaves like Redis ──────────────────────────────────


class InMemoryRedis:
    """`SET PX`, `GET`, `PTTL` and `EVAL` with real semantics.

    `eval` implements the compare-and-set contract of `tasks.CAS_TRANSITION_LUA`
    rather than returning a canned success, so a store that skips the CAS is
    caught here as well as against a live Redis.
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._expiry_ms: dict[str, float] = {}

    def _expired(self, name: str) -> bool:
        deadline = self._expiry_ms.get(name)
        return deadline is not None and deadline <= time.monotonic() * 1000

    async def set(self, name: str, value: str, px: int | None = None) -> bool:
        self._values[name] = value
        if px is not None:
            self._expiry_ms[name] = time.monotonic() * 1000 + px
        return True

    async def get(self, name: str) -> str | None:
        if name not in self._values or self._expired(name):
            return None
        return self._values[name]

    async def pttl(self, name: str) -> int:
        if name not in self._values:
            return -2
        deadline = self._expiry_ms.get(name)
        if deadline is None:
            return -1
        return int(deadline - time.monotonic() * 1000)

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        key = keys_and_args[0]
        expected_state, new_record, ttl_ms = keys_and_args[1], keys_and_args[2], keys_and_args[3]
        current = await self.get(key)
        if current is None:
            return -1
        if json.loads(current).get("state") != expected_state:
            return 0
        await self.set(key, new_record, px=int(ttl_ms))
        return 1


# ── Stub network: OPA and the upstream MCP server ────────────────────────────


class StubNetwork:
    """Routes OPA and upstream MCP calls, counting every upstream invocation."""

    def __init__(self, *, agent_blast_radius: str = "read_only") -> None:
        self.agent_blast_radius = agent_blast_radius
        self.upstream_list_calls = 0
        self.upstream_call_calls = 0
        self.opa_inputs: list[dict[str, Any]] = []
        self.opa_undefined = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in (DEFAULT_FILTER_PATH, DEFAULT_ALLOW_PATH):
            return self._opa(path, request)
        if path.endswith("/mcp"):
            return self._upstream(request)
        if path == "/.well-known/openid-configuration":
            return self._discovery(request)
        raise AssertionError(f"unexpected request to {request.url}")

    def _discovery(self, request: httpx.Request) -> httpx.Response:
        """The issuer's OIDC metadata, naming the loopback JWKS server's real path.

        Added with D-58: the verifier no longer guesses where an issuer keeps its keys, it
        reads `jwks_uri` out of this document — over the SHARED httpx client, which this
        transport routes. Before D-58 nothing fetched it, so the router had no case for it
        and every gateway request through the real graph became a 500.

        Answered rather than 404'd on purpose. Production's issuer publishes metadata, so
        answering it means the gateway suite exercises the code path a deployment takes;
        `test_jwks_discovery.py` covers the no-metadata fallback in isolation.
        """
        issuer = f"{request.url.scheme}://{request.url.netloc.decode()}"
        return httpx.Response(
            200,
            json={
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/.well-known/jwks.json",
            },
        )

    # Mirrors policies/mcp/gateway.rego closely enough to exercise the client.
    def _opa(self, path: str, request: httpx.Request) -> httpx.Response:
        opa_input = json.loads(request.content)["input"]
        self.opa_inputs.append(opa_input)
        if self.opa_undefined:
            # Exactly what OPA returns for an undefined document: 200 and no result.
            return httpx.Response(200, json={})

        rank = {"read_only": 0, "workspace": 1, "infrastructure": 2}
        granted = rank[opa_input["agent_blast_radius"]]

        def allowed(tool: dict[str, Any]) -> bool:
            radius = (tool.get("annotations") or {}).get("blast_radius", "infrastructure")
            return rank[radius] <= granted

        if path == DEFAULT_FILTER_PATH:
            return httpx.Response(200, json={"result": [t for t in opa_input["tools"] if allowed(t)]})

        named = [t for t in opa_input["tools"] if t.get("name") == opa_input["tool"]]
        return httpx.Response(200, json={"result": bool(named) and allowed(named[0])})

    def _upstream(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "tools/list":
            self.upstream_list_calls += 1
            return httpx.Response(200, json={"result": {"tools": UPSTREAM_TOOLS, "ttlMs": 30_000}})
        self.upstream_call_calls += 1
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": "ok"}]}})


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, private_key.public_key()


@pytest.fixture()
def jwks_server(keypair) -> Iterator[str]:
    """Serve a REAL JWKS document over loopback and yield the issuer URL.

    Why a real server rather than a MagicMock JWKS client. `PyJWKClient` fetches
    with `urllib`, not with the shared `httpx` client, so it cannot be routed
    through `httpx.MockTransport` like every other outbound call in this file. The
    previous version therefore built `MagicMock()` signing-key and JWKS-client
    objects and poked them into `verifier._jwks_clients` — which substituted a
    collaborator (banned by design.md 0.4.1, mechanically by
    `scripts/check-test-doubles.py` FO-TD004), reached into a private attribute,
    and skipped `_get_jwks_client` and the real signature verification entirely.

    A local fixture HTTP server is explicitly the permitted substitution: it is a
    transport, the verifier is unmodified, and the JWKS fetch, the `kid` lookup and
    the RS256 signature check all run for real.
    """
    _, public_key = keypair
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk.update({"kid": KEY_ID, "use": "sig", "alg": "RS256"})
    document = json.dumps({"keys": [jwk]}).encode("utf-8")

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path != "/.well-known/jwks.json":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, *args: object) -> None:
            """Silence the default stderr access log."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def bearer(keypair, jwks_server: str) -> str:
    private_pem, _ = keypair
    now = int(time.time())
    token = pyjwt.encode(
        {"iss": jwks_server, "sub": "user-42", "aud": AUDIENCE, "exp": now + 3600, "iat": now},
        private_pem,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )
    return f"Bearer {token}"


@pytest.fixture()
def network() -> StubNetwork:
    return StubNetwork()


@pytest.fixture()
def redis() -> InMemoryRedis:
    return InMemoryRedis()


@pytest.fixture()
def client(keypair, network: StubNetwork, redis: InMemoryRedis, jwks_server: str):
    """Compose the graph exactly as src/main.py does, then mount the real router."""
    shared_http = httpx.AsyncClient(transport=httpx.MockTransport(network.handler), timeout=5.0)

    registry = McpServerRegistry.from_config(
        [
            {
                "name": "agent",
                "url": UPSTREAM_URL,
                "description": "agent",
                "capabilities": ["tools/list", "tools/call"],
            }
        ]
    )

    # The real verifier against the real local JWKS document: no double, no private
    # attribute assignment, and _get_jwks_client plus the RS256 check both run.
    verifier = OidcTokenVerifier(allowed_issuers=[jwks_server], audience=AUDIENCE, http=shared_http)

    # The real collaborators — same construction shape as src/main.py:130-145.
    policy = OpaGatewayPolicy(opa_url="http://opa.test:8181", http=shared_http)
    cache = TtlToolCache(redis, max_ttl_ms=60_000)
    upstream = McpUpstream(http=shared_http)
    task_store = RedisTaskStore(redis)

    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(mcp_router, prefix="/api/v1")
    app.state.mcp_registry = registry
    app.state.mcp_verifier = verifier
    app.state.mcp_task_store = task_store
    app.state.mcp_gateway = McpGateway(
        registry=registry,
        verifier=verifier,
        router=HeaderRouter(registry),
        policy=policy,
        cache=cache,
        upstream=upstream,
        agent_blast_radius="read_only",
    )

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _headers(bearer: str, method: str) -> dict[str, str]:
    return {"Authorization": bearer, "Mcp-Method": method, "Mcp-Name": "agent"}


# ── tools/list ───────────────────────────────────────────────────────────────


@wires("mcp_gateway", "mcp_registry", "mcp_verifier", "shared_http")
class TestToolsListThroughTheRealGraph:
    def test_returns_the_policy_filtered_tool_list(self, client, bearer, network):
        resp = client.post("/api/v1/mcp", headers=_headers(bearer, "tools/list"))

        assert resp.status_code == 200, resp.text
        names = [t["name"] for t in resp.json()["tools"]]
        # read_only agent sees the read-only tool and not the infrastructure one.
        assert names == ["agent.health"]
        assert network.upstream_list_calls == 1

    def test_opa_receives_the_input_schema_the_rego_policy_reads(self, client, bearer, network):
        client.post("/api/v1/mcp", headers=_headers(bearer, "tools/list"))

        assert network.opa_inputs, "OPA was never consulted"
        sent = network.opa_inputs[0]
        # gateway.rego reads exactly these keys; a missing one silently empties
        # the filter comprehension.
        assert sent["agent_blast_radius"] == "read_only"
        assert [t["name"] for t in sent["tools"]] == [t["name"] for t in UPSTREAM_TOOLS]
        assert sent["subject"] == "user-42"
        assert sent["server"] == "agent"

    def test_second_call_is_served_from_redis_without_a_second_upstream_fetch(self, client, bearer, network):
        first = client.post("/api/v1/mcp", headers=_headers(bearer, "tools/list"))
        second = client.post("/api/v1/mcp", headers=_headers(bearer, "tools/list"))

        assert first.json() == second.json()
        assert network.upstream_list_calls == 1, "the cache did not absorb the second list"
        # The filter runs on EVERY response, cache hit included (design §11.4).
        assert len(network.opa_inputs) == 2

    def test_the_cached_value_round_trips_as_a_tool_list(self, client, bearer, redis):
        client.post("/api/v1/mcp", headers=_headers(bearer, "tools/list"))

        cache = TtlToolCache(redis)
        stored = json.loads(redis._values[cache.key_for("agent")])
        assert [t["name"] for t in stored] == [t["name"] for t in UPSTREAM_TOOLS]

    def test_an_undefined_policy_document_fails_loudly_not_as_an_empty_list(self, client, bearer, network):
        network.opa_undefined = True

        resp = client.post("/api/v1/mcp", headers=_headers(bearer, "tools/list"))

        assert resp.status_code == 503, resp.text
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert "mcp-policy-undefined" in resp.json()["type"]


# ── tools/call ───────────────────────────────────────────────────────────────


@wires("mcp_gateway", "mcp_app_registry")
class TestToolsCallThroughTheRealGraph:
    def _call(self, client, bearer, tool: str):
        return client.post(
            "/api/v1/mcp",
            headers=_headers(bearer, "tools/call"),
            content=json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool}}),
        )

    def test_allowed_tool_dispatches_exactly_once(self, client, bearer, network):
        # Prime the cache so metadata resolution can see the tool's annotations.
        client.post("/api/v1/mcp", headers=_headers(bearer, "tools/list"))

        resp = self._call(client, bearer, "agent.health")

        assert resp.status_code == 200, resp.text
        assert resp.json()["content"][0]["text"] == "ok"
        assert network.upstream_call_calls == 1

    def test_denied_tool_returns_403_with_zero_upstream_calls(self, client, bearer, network):
        client.post("/api/v1/mcp", headers=_headers(bearer, "tools/list"))

        resp = self._call(client, bearer, "agent.tofu.apply")

        assert resp.status_code == 403, resp.text
        assert network.upstream_call_calls == 0, "a denied call reached the upstream (P-05)"

    def test_unknown_tool_defaults_to_the_highest_blast_radius_and_is_denied(self, client, bearer, network):
        client.post("/api/v1/mcp", headers=_headers(bearer, "tools/list"))

        resp = self._call(client, bearer, "agent.totally.unknown")

        assert resp.status_code == 403, resp.text
        assert network.upstream_call_calls == 0

    def test_no_bearer_token_is_401_before_any_upstream_work(self, client, network):
        resp = client.post(
            "/api/v1/mcp",
            headers={"Mcp-Method": "tools/call", "Mcp-Name": "agent"},
            content=json.dumps({"params": {"name": "agent.health"}}),
        )

        assert resp.status_code == 401
        assert network.upstream_call_calls == 0
        assert network.upstream_list_calls == 0

    def test_malformed_body_is_400_before_any_upstream_work(self, client, bearer, network):
        resp = client.post("/api/v1/mcp", headers=_headers(bearer, "tools/call"), content=b"{not json")

        assert resp.status_code == 400
        assert network.upstream_call_calls == 0


# ── tasks lifecycle ──────────────────────────────────────────────────────────


@wires("mcp_task_store")
class TestTasksLifecycleThroughTheRealGraph:
    def _tasks(self, client, bearer, method: str, params: dict[str, Any]):
        return client.post(
            "/api/v1/mcp",
            headers=_headers(bearer, method),
            content=json.dumps({"jsonrpc": "2.0", "method": method, "params": params}),
        )

    def test_create_poll_cancel(self, client, bearer):
        created = self._tasks(client, bearer, "tasks/create", {"kind": "plan"})
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["state"] == "submitted"
        assert body["kind"] == "plan"
        task_id = body["task_id"]

        polled = self._tasks(client, bearer, "tasks/get", {"id": task_id})
        assert polled.status_code == 200
        assert polled.json()["task_id"] == task_id

        cancelled = self._tasks(client, bearer, "tasks/cancel", {"id": task_id})
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"

        # Cancellation is idempotent: still 200, still cancelled (§11.5).
        again = self._tasks(client, bearer, "tasks/cancel", {"id": task_id})
        assert again.status_code == 200
        assert again.json()["state"] == "cancelled"

    def test_update_walks_the_state_machine(self, client, bearer):
        task_id = self._tasks(client, bearer, "tasks/create", {"kind": "plan"}).json()["task_id"]

        working = self._tasks(client, bearer, "tasks/update", {"id": task_id, "state": "working"})
        assert working.status_code == 200
        assert working.json()["state"] == "working"

        done = self._tasks(client, bearer, "tasks/update", {"id": task_id, "state": "completed"})
        assert done.status_code == 200
        assert done.json()["state"] == "completed"

    def test_terminal_state_is_absorbing_over_http(self, client, bearer):
        task_id = self._tasks(client, bearer, "tasks/create", {"kind": "plan"}).json()["task_id"]
        self._tasks(client, bearer, "tasks/cancel", {"id": task_id})

        resp = self._tasks(client, bearer, "tasks/update", {"id": task_id, "state": "working"})

        assert resp.status_code == 400, resp.text
        assert resp.headers["content-type"].startswith("application/problem+json")

    def test_unknown_task_is_404_problem_json(self, client, bearer):
        resp = self._tasks(client, bearer, "tasks/get", {"id": "does-not-exist"})

        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/problem+json")

    def test_tasks_require_a_verified_token(self, client):
        resp = client.post(
            "/api/v1/mcp",
            headers={"Mcp-Method": "tasks/create", "Mcp-Name": "agent"},
            content=json.dumps({"params": {"kind": "plan"}}),
        )
        assert resp.status_code == 401
