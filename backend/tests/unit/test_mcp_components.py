# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for MCP gateway components: cache, tasks, policy, apps, server template, plan endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from src.core.errors import ProblemException

# ---------------------------------------------------------------------------
# Fake Redis for testing
# ---------------------------------------------------------------------------


class FakeRedis:
    """In-memory fake Redis for unit testing."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, int | None]] = {}  # key -> (value, ttl_ms)

    async def set(self, name: str, value: str, px: int | None = None) -> None:
        self._store[name] = (value, px)

    async def get(self, name: str) -> str | None:
        if name not in self._store:
            return None
        return self._store[name][0]

    async def pttl(self, name: str) -> int:
        if name not in self._store:
            return -2  # key does not exist
        _, ttl = self._store[name]
        if ttl is None:
            return -1  # no expiry
        return ttl  # Simulate positive TTL

    async def eval(self, script: str, numkeys: int, *keys_and_args) -> int:
        """Faithfully emulate the CAS_TRANSITION_LUA script."""
        import json as _json

        key = keys_and_args[0]
        expected_state = keys_and_args[1]
        new_record_json = keys_and_args[2]
        ttl_ms_str = keys_and_args[3]

        if key not in self._store:
            return -1

        current_json = self._store[key][0]
        current = _json.loads(current_json)
        if current["state"] != expected_state:
            return 0

        self._store[key] = (new_record_json, int(ttl_ms_str))
        return 1


class ErrorRedis:
    """Redis that always raises on every operation."""

    async def set(self, name: str, value: str, px: int | None = None) -> None:
        raise ConnectionError("Redis connection refused")

    async def get(self, name: str) -> str | None:
        raise ConnectionError("Redis connection refused")

    async def pttl(self, name: str) -> int:
        raise ConnectionError("Redis connection refused")


# ---------------------------------------------------------------------------
# Task 12.2: TtlToolCache tests
# ---------------------------------------------------------------------------


class TestTtlToolCache:
    """Tests for Redis-authoritative TTL cache."""

    async def test_put_and_get_with_valid_ttl(self):
        """Put a value and retrieve it back."""
        from src.mcp.cache import TtlToolCache

        redis = FakeRedis()
        cache = TtlToolCache(redis, max_ttl_ms=60_000)

        tools = [{"name": "tool1"}, {"name": "tool2"}]
        stored = await cache.put("terraform", tools, server_ttl_ms=30_000)
        assert stored is True

        result = await cache.get("terraform")
        assert result == tools

    async def test_put_respects_max_ttl(self):
        """Effective TTL = min(server_ttl_ms, max_ttl_ms)."""
        from src.mcp.cache import TtlToolCache

        redis = FakeRedis()
        cache = TtlToolCache(redis, max_ttl_ms=10_000)

        await cache.put("server1", [{"name": "t1"}], server_ttl_ms=50_000)
        # The stored TTL should be 10_000 (the max)
        _, ttl = redis._store[cache.key_for("server1")]
        assert ttl == 10_000

    async def test_put_with_smaller_server_ttl(self):
        """When server TTL is smaller than max, it is used."""
        from src.mcp.cache import TtlToolCache

        redis = FakeRedis()
        cache = TtlToolCache(redis, max_ttl_ms=60_000)

        await cache.put("server2", [{"name": "t2"}], server_ttl_ms=5_000)
        _, ttl = redis._store[cache.key_for("server2")]
        assert ttl == 5_000

    async def test_non_positive_ttl_creates_no_key(self):
        """Non-positive TTL → no key stored."""
        from src.mcp.cache import TtlToolCache

        redis = FakeRedis()
        cache = TtlToolCache(redis, max_ttl_ms=60_000)

        stored = await cache.put("key_zero", "val", server_ttl_ms=0)
        assert stored is False
        assert "key_zero" not in redis._store

        stored = await cache.put("key_neg", "val", server_ttl_ms=-100)
        assert stored is False
        assert "key_neg" not in redis._store

    async def test_get_returns_none_when_pttl_zero(self):
        """PTTL <= 0 → cache miss (None)."""
        from src.mcp.cache import TtlToolCache

        redis = FakeRedis()
        cache = TtlToolCache(redis, max_ttl_ms=60_000)

        # Simulate an expired key (pttl = 0)
        redis._store["expired_key"] = ("stale_value", 0)
        result = await cache.get("expired_key")
        assert result is None

    async def test_get_returns_none_for_missing_key(self):
        """Nonexistent key → None."""
        from src.mcp.cache import TtlToolCache

        redis = FakeRedis()
        cache = TtlToolCache(redis, max_ttl_ms=60_000)

        result = await cache.get("nonexistent")
        assert result is None

    async def test_redis_error_degrades_to_miss_on_put(self):
        """Redis failure on put → returns False (degrades to miss)."""
        from src.mcp.cache import TtlToolCache

        redis = ErrorRedis()
        cache = TtlToolCache(redis, max_ttl_ms=60_000)

        stored = await cache.put("key", "val", server_ttl_ms=5000)
        assert stored is False

    async def test_redis_error_degrades_to_miss_on_get(self):
        """Redis failure on get → returns None."""
        from src.mcp.cache import TtlToolCache

        redis = ErrorRedis()
        cache = TtlToolCache(redis, max_ttl_ms=60_000)

        result = await cache.get("key")
        assert result is None


# ---------------------------------------------------------------------------
# Task 12.4: Tasks state machine tests
# ---------------------------------------------------------------------------


class TestTaskStateTransitions:
    """Tests for the state machine transitions."""

    def test_allowed_transitions_from_submitted(self):
        from src.mcp.tasks import TaskState, can_transition

        assert can_transition(TaskState.SUBMITTED, TaskState.WORKING) is True
        assert can_transition(TaskState.SUBMITTED, TaskState.CANCELLED) is True
        assert can_transition(TaskState.SUBMITTED, TaskState.FAILED) is True
        assert can_transition(TaskState.SUBMITTED, TaskState.COMPLETED) is False
        assert can_transition(TaskState.SUBMITTED, TaskState.INPUT_REQUIRED) is False

    def test_allowed_transitions_from_working(self):
        from src.mcp.tasks import TaskState, can_transition

        assert can_transition(TaskState.WORKING, TaskState.INPUT_REQUIRED) is True
        assert can_transition(TaskState.WORKING, TaskState.COMPLETED) is True
        assert can_transition(TaskState.WORKING, TaskState.FAILED) is True
        assert can_transition(TaskState.WORKING, TaskState.CANCELLED) is True
        assert can_transition(TaskState.WORKING, TaskState.SUBMITTED) is False

    def test_allowed_transitions_from_input_required(self):
        from src.mcp.tasks import TaskState, can_transition

        assert can_transition(TaskState.INPUT_REQUIRED, TaskState.WORKING) is True
        assert can_transition(TaskState.INPUT_REQUIRED, TaskState.CANCELLED) is True
        assert can_transition(TaskState.INPUT_REQUIRED, TaskState.FAILED) is True
        assert can_transition(TaskState.INPUT_REQUIRED, TaskState.COMPLETED) is False

    def test_terminal_states_allow_no_transitions(self):
        from src.mcp.tasks import TERMINAL, TaskState, can_transition

        for terminal in TERMINAL:
            for target in TaskState:
                assert can_transition(terminal, target) is False

    def test_terminal_frozenset_contains_expected(self):
        from src.mcp.tasks import TERMINAL, TaskState

        assert TaskState.COMPLETED in TERMINAL
        assert TaskState.FAILED in TERMINAL
        assert TaskState.CANCELLED in TERMINAL
        assert TaskState.SUBMITTED not in TERMINAL
        assert TaskState.WORKING not in TERMINAL


class TestRedisTaskStore:
    """Tests for RedisTaskStore."""

    async def test_create_returns_submitted_record(self):
        from src.mcp.tasks import RedisTaskStore, TaskState

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        record = await store.create(kind="terraform.plan", owner="user1")
        assert record.state == TaskState.SUBMITTED
        assert record.kind == "terraform.plan"
        assert record.owner == "user1"
        assert record.task_id is not None

    async def test_get_retrieves_created_task(self):
        from src.mcp.tasks import RedisTaskStore, TaskState

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        created = await store.create(kind="ansible.run", owner="default")
        retrieved = await store.get(created.task_id)
        assert retrieved is not None
        assert retrieved.task_id == created.task_id
        assert retrieved.state == TaskState.SUBMITTED

    async def test_get_returns_none_for_missing(self):
        from src.mcp.tasks import RedisTaskStore

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        result = await store.get("nonexistent-id")
        assert result is None

    async def test_update_valid_transition(self):
        from src.mcp.tasks import RedisTaskStore, TaskState

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        record = await store.create(kind="test.tool", owner="default")
        updated = await store.update(record.task_id, TaskState.WORKING)
        assert updated.state == TaskState.WORKING

        completed = await store.update(record.task_id, TaskState.COMPLETED, result={"ok": True})
        assert completed.state == TaskState.COMPLETED
        assert completed.result == {"ok": True}

    async def test_update_invalid_transition_raises(self):
        from src.mcp.tasks import RedisTaskStore, TaskState

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        record = await store.create(kind="test.tool", owner="default")
        # submitted → completed is invalid
        with pytest.raises(ValueError, match="Invalid transition"):
            await store.update(record.task_id, TaskState.COMPLETED)

    async def test_update_not_found_raises(self):
        from src.mcp.tasks import RedisTaskStore, TaskState

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        with pytest.raises(ValueError, match="not found"):
            await store.update("no-such-id", TaskState.WORKING)

    async def test_cancel_from_working(self):
        from src.mcp.tasks import RedisTaskStore, TaskState

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        record = await store.create(kind="test.tool", owner="default")
        await store.update(record.task_id, TaskState.WORKING)
        cancelled = await store.cancel(record.task_id)
        assert cancelled.state == TaskState.CANCELLED

    async def test_idempotent_cancel_on_terminal_completed(self):
        """Cancelling an already-completed task returns existing state without error."""
        from src.mcp.tasks import RedisTaskStore, TaskState

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        record = await store.create(kind="test.tool", owner="default")
        await store.update(record.task_id, TaskState.WORKING)
        await store.update(record.task_id, TaskState.COMPLETED, result={"done": True})

        # Idempotent cancel on terminal state
        result = await store.cancel(record.task_id)
        assert result.state == TaskState.COMPLETED  # stays completed

    async def test_idempotent_cancel_on_terminal_cancelled(self):
        """Cancelling an already-cancelled task returns existing state."""
        from src.mcp.tasks import RedisTaskStore, TaskState

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        record = await store.create(kind="test.tool", owner="default")
        await store.update(record.task_id, TaskState.WORKING)
        first = await store.cancel(record.task_id)
        assert first.state == TaskState.CANCELLED

        # Second cancel is idempotent
        second = await store.cancel(record.task_id)
        assert second.state == TaskState.CANCELLED

    async def test_cancel_not_found_raises(self):
        from src.mcp.tasks import RedisTaskStore

        redis = FakeRedis()
        store = RedisTaskStore(redis)

        with pytest.raises(ValueError, match="not found"):
            await store.cancel("ghost-id")


# ---------------------------------------------------------------------------
# Task 12.3: OPA Policy tests
# ---------------------------------------------------------------------------


class TestOpaGatewayPolicy:
    """Tests for OPA policy enforcement."""

    async def test_filter_tools_returns_opa_result(self):
        from src.mcp.policy import OpaGatewayPolicy

        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": [{"name": "allowed_tool"}]}
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        policy = OpaGatewayPolicy(opa_url="http://opa:8181", http=mock_http)
        tools = [{"name": "allowed_tool"}, {"name": "blocked_tool"}]
        result = await policy.filter_tools(
            server="terraform", tools=tools, claims={"sub": "user1"}, blast_radius="read_only"
        )

        assert result == [{"name": "allowed_tool"}]
        mock_http.post.assert_called_once()

    async def test_filter_tools_fail_closed_on_opa_down(self):
        """OPA unavailable → empty list (fail-closed)."""
        from src.mcp.policy import OpaGatewayPolicy

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=ConnectionError("OPA down"))

        policy = OpaGatewayPolicy(opa_url="http://opa:8181", http=mock_http)
        result = await policy.filter_tools(
            server="terraform",
            tools=[{"name": "tool1"}],
            claims={"sub": "user1"},
            blast_radius="read_only",
        )

        assert result == []

    async def test_authorise_call_allows(self):
        """Authorise call when OPA returns allow."""
        from src.mcp.policy import OpaGatewayPolicy

        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": True}
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        policy = OpaGatewayPolicy(opa_url="http://opa:8181", http=mock_http)
        # Should not raise
        await policy.authorise_call(
            server="terraform",
            tool="safe.tool",
            metadata={},
            claims={"sub": "user1"},
            blast_radius="read_only",
        )

    async def test_authorise_call_denies_with_403(self):
        """Denied call raises 403."""
        from src.mcp.policy import OpaGatewayPolicy

        mock_http = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": False}
        mock_resp.raise_for_status = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        policy = OpaGatewayPolicy(opa_url="http://opa:8181", http=mock_http)
        with pytest.raises(ProblemException) as exc_info:
            await policy.authorise_call(
                server="terraform",
                tool="dangerous.tool",
                metadata={},
                claims={"sub": "user1"},
                blast_radius="read_only",
            )
        assert exc_info.value.problem.status == 403

    async def test_authorise_call_denies_on_opa_failure(self):
        """OPA unavailable → deny (fail-closed)."""
        from src.mcp.policy import OpaGatewayPolicy

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=ConnectionError("OPA down"))

        policy = OpaGatewayPolicy(opa_url="http://opa:8181", http=mock_http)
        with pytest.raises(ProblemException) as exc_info:
            await policy.authorise_call(
                server="terraform",
                tool="any.tool",
                metadata={},
                claims={"sub": "user1"},
                blast_radius="read_only",
            )
        assert exc_info.value.problem.status == 403


# ---------------------------------------------------------------------------
# Task 12.5: MCP Apps tests
# ---------------------------------------------------------------------------


class TestMcpApps:
    """Tests for MCP app sandbox hosting."""

    def test_default_registry_has_agent_health(self):
        from src.mcp.apps import McpAppRegistry

        registry = McpAppRegistry()
        app = registry.get("agent-health")
        assert app is not None
        assert app.tool_name == "agent.health"
        assert app.app_id == "agent-health"

    def test_descriptor_shape(self):
        from src.mcp.apps import CSP_POLICY, SANDBOX_ATTRS, McpAppDescriptor

        desc = McpAppDescriptor(
            app_id="test-app",
            name="Test App",
            description="A test MCP app",
            tool_name="test.tool",
        )
        assert desc.app_id == "test-app"
        assert desc.csp == CSP_POLICY
        assert desc.sandbox == SANDBOX_ATTRS

    def test_csp_policy_is_restrictive(self):
        from src.mcp.apps import CSP_POLICY

        assert "default-src 'none'" in CSP_POLICY
        # 'self' rather than 'none': the ForgeOps host page legitimately frames
        # the app, so 'none' would forbid the embedding this module exists for.
        # Third-party framing is still refused.
        assert "frame-ancestors 'self'" in CSP_POLICY
        assert "frame-ancestors *" not in CSP_POLICY

    def test_sandbox_never_grants_same_origin(self):
        """Design §11.6: the iframe carries allow-scripts allow-forms and
        DELIBERATELY NOT allow-same-origin.

        Granting same-origin would return the framed app to the parent's origin,
        handing it the parent's cookies, localStorage and credentialed fetch —
        which defeats the whole point of sandboxing an app UI.
        """
        from src.mcp.apps import SANDBOX_ATTRS

        assert "allow-same-origin" not in SANDBOX_ATTRS
        assert "allow-scripts" in SANDBOX_ATTRS
        assert "allow-forms" in SANDBOX_ATTRS

    def test_list_apps(self):
        from src.mcp.apps import McpAppDescriptor, McpAppRegistry

        apps = [
            McpAppDescriptor(app_id="a", name="A", description="", tool_name="t.a"),
            McpAppDescriptor(app_id="b", name="B", description="", tool_name="t.b"),
        ]
        registry = McpAppRegistry(apps)
        listed = registry.list_apps()
        assert len(listed) == 2
        ids = {a.app_id for a in listed}
        assert ids == {"a", "b"}

    def test_get_sandbox_headers_includes_csp(self):
        from src.mcp.apps import McpAppRegistry

        registry = McpAppRegistry()
        headers = registry.get_sandbox_headers("agent-health")
        assert "Content-Security-Policy" in headers
        assert "X-Frame-Options" in headers
        assert headers["X-Frame-Options"] == "DENY"
        assert "X-Sandbox-Token" in headers
        assert len(headers["X-Sandbox-Token"]) > 0

    def test_get_sandbox_headers_unknown_app(self):
        from src.mcp.apps import McpAppRegistry

        registry = McpAppRegistry()
        headers = registry.get_sandbox_headers("unknown")
        assert headers == {}

    def test_register_new_app(self):
        from src.mcp.apps import McpAppDescriptor, McpAppRegistry

        registry = McpAppRegistry(apps=[])
        desc = McpAppDescriptor(app_id="new", name="New", description="", tool_name="n.t")
        registry.register(desc)
        assert registry.get("new") is not None


# ---------------------------------------------------------------------------
# Task 12.6: Server template tests
# ---------------------------------------------------------------------------


class TestMcpServerDispatcher:
    """Tests for the MCP server template dispatcher."""

    def test_list_tools_includes_platform_health(self):
        from src.mcp.server_template import McpServerDispatcher

        dispatcher = McpServerDispatcher()
        tools = dispatcher.list_tools()
        assert len(tools) >= 1
        names = [t["name"] for t in tools]
        assert "platform.health" in names

    def test_tool_descriptor_shape(self):
        from src.mcp.server_template import McpServerDispatcher

        dispatcher = McpServerDispatcher()
        tools = dispatcher.list_tools()
        health = next(t for t in tools if t["name"] == "platform.health")
        assert "description" in health
        assert "inputSchema" in health
        assert "annotations" in health
        assert health["annotations"]["blastRadius"] == "none"

    async def test_call_platform_health(self):
        from src.mcp.server_template import McpServerDispatcher

        dispatcher = McpServerDispatcher()
        result = await dispatcher.call_tool("platform.health", {})
        assert result["status"] == "healthy"
        assert "service" in result

    async def test_call_unknown_tool_raises_400(self):
        from src.mcp.server_template import McpServerDispatcher

        dispatcher = McpServerDispatcher()
        with pytest.raises(ProblemException) as exc_info:
            await dispatcher.call_tool("nonexistent.tool", {})
        assert exc_info.value.problem.status == 400

    async def test_call_with_invalid_input_raises_400(self):
        """Platform.health has additionalProperties=false, so extra keys fail."""
        from src.mcp.server_template import McpServerDispatcher

        dispatcher = McpServerDispatcher()
        with pytest.raises(ProblemException) as exc_info:
            await dispatcher.call_tool("platform.health", {"unexpected_key": "value"})
        assert exc_info.value.problem.status == 400
        assert "mcp-invalid-input" in exc_info.value.problem.type

    async def test_register_custom_tool(self):
        from src.mcp.server_template import McpServerDispatcher, ToolSpec

        async def my_handler(args: dict) -> dict:
            return {"echo": args.get("msg", "")}

        spec = ToolSpec(
            name="custom.echo",
            description="Echoes input",
            input_schema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
                "additionalProperties": False,
            },
            blast_radius="low",
            handler=my_handler,
        )

        dispatcher = McpServerDispatcher(tools=[spec])
        result = await dispatcher.call_tool("custom.echo", {"msg": "hello"})
        assert result == {"echo": "hello"}

    async def test_custom_tool_schema_validation(self):
        """Missing required property fails schema validation."""
        from src.mcp.server_template import McpServerDispatcher, ToolSpec

        async def handler(args: dict) -> dict:
            return {}

        spec = ToolSpec(
            name="custom.strict",
            description="Strict schema",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            handler=handler,
        )

        dispatcher = McpServerDispatcher(tools=[spec])
        with pytest.raises(ProblemException) as exc_info:
            await dispatcher.call_tool("custom.strict", {})
        assert exc_info.value.problem.status == 400


# ---------------------------------------------------------------------------
# Task 14.4: Plan analysis endpoint tests
# ---------------------------------------------------------------------------


class TestPlanAnalysisEndpoint:
    """Tests for POST /api/v1/analysis/plan."""

    @pytest.fixture()
    def client(self):
        """Create a test client with the analysis router."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.analysis.routes import router

        app = FastAPI()
        app.include_router(router)

        # The analysis router now carries `require_principal` (design §4.4). These
        # tests exercise plan analysis, not authentication, so the dependency is
        # OVERRIDDEN with a fixed principal. An override is used rather than a stub
        # verifier because it is honest about what is being skipped: a stub verifier
        # would make these look like they exercise auth when they do not. Deny-by-
        # default itself is asserted by Q-19 and scripts/check-route-auth.py.
        import uuid as _uuid

        from src.auth.dependencies import require_principal
        from src.auth.models import UserRole
        from src.auth.principal import Principal

        _principal = Principal.for_user(
            user_id=_uuid.uuid4(),
            subject="test-only-not-a-real-subject",
            email="analysis@example.invalid",
            role=UserRole.DEVELOPER,
        )
        app.dependency_overrides[require_principal] = lambda: _principal

        # Install problem handlers for proper error rendering
        from src.core.errors import install_problem_handlers

        install_problem_handlers(app)

        return TestClient(app)

    def test_valid_plan_returns_findings(self, client):
        """A valid plan with creates returns allow verdict."""
        plan = {
            "format_version": "1.0",
            "terraform_version": "1.5.0",
            "resource_changes": [
                {
                    "address": "aws_instance.web",
                    "type": "aws_instance",
                    "change": {"actions": ["create"]},
                }
            ],
        }
        resp = client.post("/api/v1/analysis/plan", json={"plan": plan})
        assert resp.status_code == 200
        body = resp.json()
        assert "findings" in body
        assert "verdict" in body
        assert body["verdict"] == "allow"
        assert body["blast_radius"] is not None
        assert body["blast_radius"]["score"] > 0
        assert body["approval_decision"] == "auto_ok"

    def test_destructive_plan_returns_warn_or_block(self, client):
        """A plan with deletions returns warn/block verdict."""
        plan = {
            "format_version": "1.0",
            "terraform_version": "1.5.0",
            "resource_changes": [
                {
                    "address": "aws_db_instance.main",
                    "type": "aws_db_instance",
                    "change": {"actions": ["delete"]},
                }
            ],
        }
        resp = client.post("/api/v1/analysis/plan", json={"plan": plan})
        assert resp.status_code == 200
        body = resp.json()
        assert body["verdict"] == "block"
        assert body["approval_decision"] == "blocked"
        assert body["blast_radius"]["destructive_count"] == 1
        assert "aws_db_instance.main" in body["blast_radius"]["stateful_deletions"]

    def test_empty_plan_returns_findings(self, client):
        """An empty plan triggers fatal syntax finding."""
        plan = {}
        resp = client.post("/api/v1/analysis/plan", json={"plan": plan})
        assert resp.status_code == 200
        body = resp.json()
        assert body["verdict"] == "fatal"
        assert any(f["code"] == "EMPTY_PLAN" for f in body["findings"])

    def test_malformed_request_returns_422(self, client):
        """Missing required 'plan' field returns validation error."""
        resp = client.post("/api/v1/analysis/plan", json={"not_plan": "data"})
        assert resp.status_code == 422

    def test_plan_with_no_resource_changes(self, client):
        """Plan with no resource changes is a no-op."""
        plan = {
            "format_version": "1.0",
            "terraform_version": "1.5.0",
            "resource_changes": [],
        }
        resp = client.post("/api/v1/analysis/plan", json={"plan": plan})
        assert resp.status_code == 200
        body = resp.json()
        assert body["verdict"] == "allow"
        assert body["blast_radius"]["score"] == 0

    def test_plan_with_multiple_resources(self, client):
        """Plan with mixed create and delete actions."""
        plan = {
            "format_version": "1.0",
            "terraform_version": "1.5.0",
            "resource_changes": [
                {
                    "address": "aws_instance.app",
                    "type": "aws_instance",
                    "change": {"actions": ["create"]},
                },
                {
                    "address": "aws_security_group.old",
                    "type": "aws_security_group",
                    "change": {"actions": ["delete"]},
                },
            ],
        }
        resp = client.post("/api/v1/analysis/plan", json={"plan": plan})
        assert resp.status_code == 200
        body = resp.json()
        assert body["blast_radius"]["affected_resources"] == 2
        assert body["blast_radius"]["destructive_count"] == 1
