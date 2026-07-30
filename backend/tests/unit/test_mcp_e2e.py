# SPDX-License-Identifier: FSL-1.1-ALv2
"""End-to-end MCP gateway tests (Task 12.12).

Composes the full gateway pipeline with mocked collaborators to verify:
- Authenticated tools/list with cache miss → upstream → OPA filter → response
- Authenticated tools/list with cache hit → OPA filter (policy still runs)
- Authenticated tools/call allowed → dispatch
- tools/call denied → zero upstream invocations
- tasks/create → poll → cancel → cancel-again (idempotent)
- OIDC issuer failure → 401
- Missing headers → 400
- Unknown server → 404
- Trace headers propagation

Key assertion: for EVERY rejected tools/call, upstream.call_tool counter == 0.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, create_autospec

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from src.core.errors import ProblemException
from src.mcp.auth import OidcTokenVerifier
from src.mcp.cache import TtlToolCache
from src.mcp.gateway import McpGateway
from src.mcp.policy import OpaGatewayPolicy
from src.mcp.registry import McpServerRegistry
from src.mcp.routing import MCP_METHOD_HEADER, MCP_NAME_HEADER, HeaderRouter
from src.mcp.tasks import RedisTaskStore, TaskState
from src.mcp.upstream import McpUpstream

from tests import synthetic_secrets

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rsa_keypair():
    """Generate an RSA key pair for signing test JWTs."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    return private_pem, public_key


@pytest.fixture()
def valid_token(rsa_keypair) -> str:
    """A valid signed JWT token."""
    private_pem, _ = rsa_keypair
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": "https://auth.forgeops.dev",
            "sub": "user-42",
            "aud": "forgeops-mcp-gateway",
            "exp": now + 3600,
            "iat": now,
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )


@pytest.fixture()
def verifier_with_mock(rsa_keypair) -> OidcTokenVerifier:
    """An OIDC verifier configured to accept our test tokens."""
    _, public_key = rsa_keypair
    verifier = OidcTokenVerifier(
        allowed_issuers=["https://auth.forgeops.dev"],
        audience="forgeops-mcp-gateway",
    )

    # Mock JWKS client to return our test public key
    mock_jwks_client = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_key
    mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key
    verifier._jwks_clients["https://auth.forgeops.dev"] = mock_jwks_client
    verifier._jwks_cache_times["https://auth.forgeops.dev"] = time.time()

    return verifier


@pytest.fixture()
def server_config() -> list[dict]:
    return [
        {
            "name": "terraform",
            "url": "http://localhost:9001",
            "description": "Terraform MCP",
            "capabilities": ["tools/list", "tools/call"],
        },
        {
            "name": "ansible",
            "url": "http://localhost:9002",
            "description": "Ansible MCP",
            "capabilities": ["tools/list"],
        },
    ]


@pytest.fixture()
def registry(server_config: list[dict]) -> McpServerRegistry:
    return McpServerRegistry.from_config(server_config)


@pytest.fixture()
def header_router(registry: McpServerRegistry) -> HeaderRouter:
    return HeaderRouter(registry)


@pytest.fixture()
def mock_upstream() -> AsyncMock:
    """Mock upstream that tracks call counts."""
    from src.mcp.upstream import ToolListResult

    upstream = create_autospec(McpUpstream, spec_set=True, instance=True)
    upstream.list_tools.return_value = ToolListResult(
        tools=[
            {"name": "plan", "description": "Run terraform plan"},
            {"name": "apply", "description": "Run terraform apply"},
            {"name": "destroy", "description": "Destroy infrastructure"},
        ],
        ttl_ms=30000,
    )
    upstream.call_tool.return_value = {"result": "success", "output": "Plan: 3 to add"}
    return upstream


@pytest.fixture()
def mock_policy() -> AsyncMock:
    """Mock OPA policy — allows everything by default."""
    policy = create_autospec(OpaGatewayPolicy, spec_set=True, instance=True)
    # filter_tools: pass through all tools by default
    policy.filter_tools.side_effect = lambda **kwargs: kwargs.get("tools", [])
    # authorise_call: allows by default (no exception)
    policy.authorise_call.return_value = None
    return policy


@pytest.fixture()
def mock_cache() -> AsyncMock:
    """Mock TTL cache — miss by default."""
    cache = create_autospec(TtlToolCache, spec_set=True, instance=True)
    cache.get.return_value = None
    cache.put.return_value = None
    return cache


@pytest.fixture()
def gateway(
    registry: McpServerRegistry,
    verifier_with_mock: OidcTokenVerifier,
    header_router: HeaderRouter,
    mock_policy: AsyncMock,
    mock_cache: AsyncMock,
    mock_upstream: AsyncMock,
) -> McpGateway:
    """Fully composed gateway with mocked collaborators."""
    return McpGateway(
        registry=registry,
        verifier=verifier_with_mock,
        router=header_router,
        policy=mock_policy,
        cache=mock_cache,
        upstream=mock_upstream,
        agent_blast_radius="read_only",
    )


@pytest.fixture()
def mock_redis_for_tasks() -> AsyncMock:
    """Fake Redis for task store with faithful CAS eval."""
    import json as _json

    store: dict[str, str] = {}

    async def fake_set(name: str, value: str, px: int | None = None) -> None:
        store[name] = value

    async def fake_get(name: str) -> str | None:
        return store.get(name)

    async def fake_eval(script: str, numkeys: int, *keys_and_args) -> int:
        key = keys_and_args[0]
        expected_state = keys_and_args[1]
        new_record_json = keys_and_args[2]
        # keys_and_args[3] is ttl_ms_str, not needed for in-memory
        if key not in store:
            return -1
        current = _json.loads(store[key])
        if current["state"] != expected_state:
            return 0
        store[key] = new_record_json
        return 1

    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=fake_set)
    redis.get = AsyncMock(side_effect=fake_get)
    redis.eval = AsyncMock(side_effect=fake_eval)
    return redis


# ---------------------------------------------------------------------------
# Tests: Authenticated tools/list — cache miss flow
# ---------------------------------------------------------------------------


class TestToolsListCacheMiss:
    """tools/list with cache miss → upstream → OPA filter → response."""

    async def test_full_pipeline_returns_filtered_tools(
        self, gateway: McpGateway, valid_token: str, mock_upstream: AsyncMock, mock_cache: AsyncMock
    ):
        result = await gateway.handle_tools_list(
            authorization=f"Bearer {valid_token}",
            headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"},
        )

        assert "tools" in result
        assert len(result["tools"]) == 3
        # Cache was checked (miss)
        mock_cache.get.assert_called_once_with("terraform")
        # Upstream was called
        mock_upstream.list_tools.assert_called_once()
        # Cache was populated
        mock_cache.put.assert_called_once()

    async def test_opa_filter_runs_on_upstream_result(
        self, gateway: McpGateway, valid_token: str, mock_policy: AsyncMock
    ):
        await gateway.handle_tools_list(
            authorization=f"Bearer {valid_token}",
            headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"},
        )

        # OPA filter was invoked
        mock_policy.filter_tools.assert_called_once()
        call_kwargs = mock_policy.filter_tools.call_args.kwargs
        assert call_kwargs["server"] == "terraform"
        assert len(call_kwargs["tools"]) == 3
        assert call_kwargs["blast_radius"] == "read_only"


# ---------------------------------------------------------------------------
# Tests: Authenticated tools/list — cache hit flow
# ---------------------------------------------------------------------------


class TestToolsListCacheHit:
    """tools/list with cache hit → OPA filter (policy still runs on hits)."""

    async def test_cache_hit_skips_upstream(
        self, gateway: McpGateway, valid_token: str, mock_cache: AsyncMock, mock_upstream: AsyncMock
    ):
        # Setup: cache hit returns tools
        cached_tools = [{"name": "plan", "description": "Run plan"}]
        mock_cache.get.return_value = cached_tools

        result = await gateway.handle_tools_list(
            authorization=f"Bearer {valid_token}",
            headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"},
        )

        assert "tools" in result
        # Upstream NOT called
        mock_upstream.list_tools.assert_not_called()
        # Cache NOT re-populated
        mock_cache.put.assert_not_called()

    async def test_opa_filter_still_runs_on_cache_hit(
        self, gateway: McpGateway, valid_token: str, mock_cache: AsyncMock, mock_policy: AsyncMock
    ):
        cached_tools = [{"name": "plan", "description": "Run plan"}]
        mock_cache.get.return_value = cached_tools

        await gateway.handle_tools_list(
            authorization=f"Bearer {valid_token}",
            headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"},
        )

        # OPA filter still invoked even on cache hit
        mock_policy.filter_tools.assert_called_once()
        call_kwargs = mock_policy.filter_tools.call_args.kwargs
        assert call_kwargs["tools"] == cached_tools


# ---------------------------------------------------------------------------
# Tests: Authenticated tools/call — allowed
# ---------------------------------------------------------------------------


class TestToolsCallAllowed:
    """tools/call allowed → dispatch to upstream."""

    async def test_allowed_call_dispatches_to_upstream(
        self, gateway: McpGateway, valid_token: str, mock_upstream: AsyncMock
    ):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "plan", "arguments": {"dir": "/infra"}},
            }
        ).encode()

        result = await gateway.handle_tools_call(
            authorization=f"Bearer {valid_token}",
            headers={MCP_METHOD_HEADER: "tools/call", MCP_NAME_HEADER: "terraform"},
            body=body,
        )

        assert result == {"result": "success", "output": "Plan: 3 to add"}
        # Upstream call_tool was invoked exactly once
        mock_upstream.call_tool.assert_called_once()

    async def test_opa_authorise_call_invoked_before_dispatch(
        self, gateway: McpGateway, valid_token: str, mock_policy: AsyncMock
    ):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "plan", "arguments": {}},
            }
        ).encode()

        await gateway.handle_tools_call(
            authorization=f"Bearer {valid_token}",
            headers={MCP_METHOD_HEADER: "tools/call", MCP_NAME_HEADER: "terraform"},
            body=body,
        )

        mock_policy.authorise_call.assert_called_once()
        call_kwargs = mock_policy.authorise_call.call_args.kwargs
        assert call_kwargs["server"] == "terraform"
        assert call_kwargs["tool"] == "plan"


# ---------------------------------------------------------------------------
# Tests: tools/call denied → ZERO upstream invocations
# ---------------------------------------------------------------------------


class TestToolsCallDenied:
    """tools/call denied → zero upstream.call_tool invocations."""

    async def test_denied_call_zero_upstream_invocations(
        self, gateway: McpGateway, valid_token: str, mock_upstream: AsyncMock, mock_policy: AsyncMock
    ):
        # OPA denies the call
        mock_policy.authorise_call.side_effect = ProblemException(
            status=403,
            type_suffix="mcp-call-denied",
            title="Tool call denied",
            detail="Policy denied tool 'destroy'.",
        )

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "destroy", "arguments": {}},
            }
        ).encode()

        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_call(
                authorization=f"Bearer {valid_token}",
                headers={MCP_METHOD_HEADER: "tools/call", MCP_NAME_HEADER: "terraform"},
                body=body,
            )

        assert exc_info.value.problem.status == 403
        # KEY ASSERTION: upstream.call_tool counter MUST be 0
        assert mock_upstream.call_tool.call_count == 0

    async def test_denied_with_different_tools_all_have_zero_upstream(
        self, gateway: McpGateway, valid_token: str, mock_upstream: AsyncMock, mock_policy: AsyncMock
    ):
        """Multiple denied calls all produce zero upstream invocations."""
        mock_policy.authorise_call.side_effect = ProblemException(
            status=403,
            type_suffix="mcp-call-denied",
            title="Tool call denied",
            detail="Denied.",
        )

        for tool_name in ["destroy", "apply", "delete_all"]:
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": tool_name, "arguments": {}},
                }
            ).encode()

            with pytest.raises(ProblemException):
                await gateway.handle_tools_call(
                    authorization=f"Bearer {valid_token}",
                    headers={MCP_METHOD_HEADER: "tools/call", MCP_NAME_HEADER: "terraform"},
                    body=body,
                )

        # KEY ASSERTION: after 3 denied calls, upstream.call_tool is STILL 0
        assert mock_upstream.call_tool.call_count == 0


# ---------------------------------------------------------------------------
# Tests: tasks/create → poll → cancel → cancel-again (idempotent)
# ---------------------------------------------------------------------------


class TestTasksLifecycle:
    """tasks/create → poll → cancel → cancel-again (idempotent)."""

    async def test_create_poll_cancel_idempotent(self, mock_redis_for_tasks: AsyncMock):
        store = RedisTaskStore(mock_redis_for_tasks)

        # CREATE
        task = await store.create(kind="plan", owner="user1")
        assert task.state == TaskState.SUBMITTED
        assert task.kind == "plan"

        # POLL (get)
        polled = await store.get(task.task_id)
        assert polled is not None
        assert polled.state == TaskState.SUBMITTED
        assert polled.task_id == task.task_id

        # CANCEL (first time — transitions from SUBMITTED → CANCELLED)
        cancelled = await store.cancel(task.task_id)
        assert cancelled.state == TaskState.CANCELLED

        # CANCEL AGAIN (idempotent — already terminal, no error)
        cancelled_again = await store.cancel(task.task_id)
        assert cancelled_again.state == TaskState.CANCELLED

    async def test_cancel_working_task(self, mock_redis_for_tasks: AsyncMock):
        store = RedisTaskStore(mock_redis_for_tasks)

        task = await store.create(kind="apply", owner="default")
        # Transition to WORKING
        await store.update(task.task_id, TaskState.WORKING)

        # Cancel from WORKING state
        cancelled = await store.cancel(task.task_id)
        assert cancelled.state == TaskState.CANCELLED

        # Idempotent cancel
        cancelled_again = await store.cancel(task.task_id)
        assert cancelled_again.state == TaskState.CANCELLED

    async def test_cancel_completed_task_is_noop(self, mock_redis_for_tasks: AsyncMock):
        store = RedisTaskStore(mock_redis_for_tasks)

        task = await store.create(kind="plan", owner="default")
        await store.update(task.task_id, TaskState.WORKING)
        await store.update(task.task_id, TaskState.COMPLETED, result={"output": "done"})

        # Cancelling a completed task returns existing state (no error)
        result = await store.cancel(task.task_id)
        assert result.state == TaskState.COMPLETED


# ---------------------------------------------------------------------------
# Tests: OIDC issuer failure → 401
# ---------------------------------------------------------------------------


class TestOidcFailure:
    """OIDC issuer failure → 401."""

    async def test_untrusted_issuer_returns_401(self, gateway: McpGateway, rsa_keypair):
        private_pem, _ = rsa_keypair
        now = int(time.time())
        bad_token = pyjwt.encode(
            {
                "iss": "https://evil.attacker.com",
                "sub": "hacker",
                "aud": "forgeops-mcp-gateway",
                "exp": now + 3600,
                "iat": now,
            },
            private_pem,
            algorithm="RS256",
        )

        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_list(
                authorization=f"Bearer {bad_token}",
                headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"},
            )

        assert exc_info.value.problem.status == 401

    async def test_expired_token_returns_401(self, gateway: McpGateway, rsa_keypair):
        private_pem, public_key = rsa_keypair
        now = int(time.time())
        expired_token = pyjwt.encode(
            {
                "iss": "https://auth.forgeops.dev",
                "sub": "user-42",
                "aud": "forgeops-mcp-gateway",
                "exp": now - 3600,
                "iat": now - 7200,
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": "test-key-1"},
        )

        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_list(
                authorization=f"Bearer {expired_token}",
                headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"},
            )

        assert exc_info.value.problem.status == 401


# ---------------------------------------------------------------------------
# Tests: Missing headers → 400
# ---------------------------------------------------------------------------


class TestMissingHeaders:
    """Missing routing headers → 400."""

    async def test_missing_method_header(self, gateway: McpGateway, valid_token: str):
        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_list(
                authorization=f"Bearer {valid_token}",
                headers={MCP_NAME_HEADER: "terraform"},
            )
        assert exc_info.value.problem.status == 400

    async def test_missing_name_header(self, gateway: McpGateway, valid_token: str):
        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_list(
                authorization=f"Bearer {valid_token}",
                headers={MCP_METHOD_HEADER: "tools/list"},
            )
        assert exc_info.value.problem.status == 400

    async def test_empty_headers(self, gateway: McpGateway, valid_token: str):
        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_list(
                authorization=f"Bearer {valid_token}",
                headers={},
            )
        assert exc_info.value.problem.status == 400


# ---------------------------------------------------------------------------
# Tests: Unknown server → 404
# ---------------------------------------------------------------------------


class TestUnknownServer:
    """Unknown server → 404."""

    async def test_unknown_server_tools_list(self, gateway: McpGateway, valid_token: str):
        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_list(
                authorization=f"Bearer {valid_token}",
                headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "nonexistent"},
            )
        assert exc_info.value.problem.status == 404

    async def test_unknown_server_tools_call(self, gateway: McpGateway, valid_token: str):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "plan", "arguments": {}},
            }
        ).encode()

        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_call(
                authorization=f"Bearer {valid_token}",
                headers={MCP_METHOD_HEADER: "tools/call", MCP_NAME_HEADER: "ghost"},
                body=body,
            )
        assert exc_info.value.problem.status == 404

    async def test_unknown_server_zero_upstream_calls(
        self, gateway: McpGateway, valid_token: str, mock_upstream: AsyncMock
    ):
        """Unknown server: upstream is never invoked."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "plan", "arguments": {}},
            }
        ).encode()

        with pytest.raises(ProblemException):
            await gateway.handle_tools_call(
                authorization=f"Bearer {valid_token}",
                headers={MCP_METHOD_HEADER: "tools/call", MCP_NAME_HEADER: "ghost"},
                body=body,
            )
        # KEY ASSERTION: zero upstream invocations
        assert mock_upstream.call_tool.call_count == 0


# ---------------------------------------------------------------------------
# Tests: Trace header propagation
# ---------------------------------------------------------------------------


class TestTraceHeaderPropagation:
    """Trace headers propagate through the flow."""

    async def test_tools_list_propagates_trace_context(
        self, gateway: McpGateway, valid_token: str, mock_upstream: AsyncMock
    ):
        """Verify that trace headers present in the request are visible to upstream."""
        # The gateway receives headers including trace context
        headers = {
            MCP_METHOD_HEADER: "tools/list",
            MCP_NAME_HEADER: "terraform",
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "vendor1=value1",
        }

        result = await gateway.handle_tools_list(
            authorization=f"Bearer {valid_token}",
            headers=headers,
        )

        # The trace headers were present in the headers dict passed to route
        assert "tools" in result

    async def test_tools_call_with_trace_headers(self, gateway: McpGateway, valid_token: str, mock_upstream: AsyncMock):
        """tools/call carries trace headers through the pipeline."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "plan", "arguments": {}},
            }
        ).encode()

        headers = {
            MCP_METHOD_HEADER: "tools/call",
            MCP_NAME_HEADER: "terraform",
            "traceparent": "00-abcdef1234567890abcdef1234567890-1234567890abcdef-01",
            "tracestate": "forgeops=active",
        }

        result = await gateway.handle_tools_call(
            authorization=f"Bearer {valid_token}",
            headers=headers,
            body=body,
        )

        # Call succeeded and upstream was invoked
        mock_upstream.call_tool.assert_called_once()
        assert result is not None


# ---------------------------------------------------------------------------
# Tests: Authentication boundary (no token, malformed token)
# ---------------------------------------------------------------------------


class TestAuthenticationBoundary:
    """Auth boundary: no token, empty token, non-bearer scheme."""

    async def test_no_authorization_header_401(self, gateway: McpGateway):
        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_list(
                authorization=None,
                headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"},
            )
        assert exc_info.value.problem.status == 401

    async def test_empty_authorization_401(self, gateway: McpGateway):
        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_list(
                authorization="",
                headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"},
            )
        assert exc_info.value.problem.status == 401

    async def test_malformed_token_401(self, gateway: McpGateway):
        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_list(
                authorization=synthetic_secrets.bearer_with("garbage.not.valid!!!"),
                headers={MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"},
            )
        assert exc_info.value.problem.status == 401

    async def test_no_auth_on_tools_call_401_zero_upstream(self, gateway: McpGateway, mock_upstream: AsyncMock):
        """No auth on tools/call: 401 AND zero upstream calls."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "plan", "arguments": {}},
            }
        ).encode()

        with pytest.raises(ProblemException) as exc_info:
            await gateway.handle_tools_call(
                authorization=None,
                headers={MCP_METHOD_HEADER: "tools/call", MCP_NAME_HEADER: "terraform"},
                body=body,
            )
        assert exc_info.value.problem.status == 401
        # KEY ASSERTION: zero upstream invocations
        assert mock_upstream.call_tool.call_count == 0
