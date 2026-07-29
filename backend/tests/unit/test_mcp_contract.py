# SPDX-License-Identifier: FSL-1.1-ALv2
"""Signature conformance between McpGateway and its REAL collaborators.

Why this file exists
--------------------
The gateway was shipped calling four collaborator methods with keyword names the
real classes did not accept:

    policy.filter_tools(server=..., tools=..., claims=..., blast_radius=...)
    policy.authorise_call(server=..., tool=..., metadata=..., claims=..., blast_radius=...)
    upstream.list_tools(descriptor)  -> the gateway then called .get("tools") on a list
    cache.put(name, list, None)      -> the callee expected (key, str, int)
    store.create(kind=..., owner=...)

Every one of those raised at runtime, and 419 tests stayed green because the test
doubles were built against the contract the gateway *wanted*, not the one the
collaborators *had*. Type checking did not catch it either: the collaborators are
injected through the constructor and the call sites are dynamically dispatched.

These tests bind the gateway's actual call sites against the real classes with
`inspect.signature().bind()`. They need no Redis, no OPA and no HTTP, they run in
milliseconds, and they fail the moment a caller and a callee disagree again.

Design authority: §11.4 (gateway/policy/cache/upstream) and §11.5 (task store).
The gateway's call sites are the specified contract; the collaborators conform to
them, not the other way round.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from src.mcp.cache import TtlToolCache
from src.mcp.policy import DEFAULT_ALLOW_PATH, DEFAULT_FILTER_PATH, OpaGatewayPolicy
from src.mcp.tasks import RedisTaskStore
from src.mcp.upstream import McpUpstream, ToolCall, ToolListResult

# Each entry mirrors one real call site in src/mcp/gateway.py or src/mcp/routes.py.
# `args`/`kwargs` are exactly what the caller passes.
CALL_SITES: list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]] = [
    (
        "gateway.handle_tools_list -> policy.filter_tools",
        OpaGatewayPolicy.filter_tools,
        (),
        {"server": "agent", "tools": [], "claims": {"sub": "s"}, "blast_radius": "read_only"},
    ),
    (
        "gateway.handle_tools_call -> policy.authorise_call",
        OpaGatewayPolicy.authorise_call,
        (),
        {
            "server": "agent",
            "tool": "agent.health",
            "metadata": {},
            "claims": {"sub": "s"},
            "blast_radius": "read_only",
        },
    ),
    (
        "gateway.handle_tools_list -> cache.get",
        TtlToolCache.get,
        ("agent",),
        {},
    ),
    (
        "gateway.handle_tools_list -> cache.put",
        TtlToolCache.put,
        ("agent", [{"name": "agent.health"}], None),
        {},
    ),
    (
        "gateway._resolve_metadata -> cache.get",
        TtlToolCache.get,
        ("agent",),
        {},
    ),
    (
        "gateway.handle_tools_list -> upstream.list_tools",
        McpUpstream.list_tools,
        (object(),),  # a ServerDescriptor, not a URL string
        {},
    ),
    (
        "gateway.handle_tools_call -> upstream.call_tool",
        McpUpstream.call_tool,
        (object(), ToolCall(tool="agent.health")),
        {},
    ),
    (
        "routes._handle_tasks -> store.create",
        RedisTaskStore.create,
        (),
        {"kind": "generic", "owner": "default"},
    ),
    (
        "routes._handle_tasks -> store.get",
        RedisTaskStore.get,
        ("task-id",),
        {},
    ),
    (
        "routes._handle_tasks -> store.cancel",
        RedisTaskStore.cancel,
        ("task-id",),
        {},
    ),
    (
        "routes._handle_tasks -> store.update",
        RedisTaskStore.update,
        ("task-id", "working"),
        {},
    ),
]


@pytest.mark.parametrize(
    ("label", "func", "args", "kwargs"),
    CALL_SITES,
    ids=[c[0] for c in CALL_SITES],
)
def test_gateway_call_site_binds_against_the_real_collaborator(
    label: str, func: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    """The keywords the gateway passes must bind to the real method signature."""
    signature = inspect.signature(func)
    try:
        # `None` stands in for `self`; binding only checks arity and names.
        signature.bind(None, *args, **kwargs)
    except TypeError as exc:  # pragma: no cover - the failure is the message
        pytest.fail(f"{label} does not bind against {func.__qualname__}{signature}: {exc}")


def test_upstream_list_tools_returns_a_result_object_not_a_bare_list() -> None:
    """The gateway reads `.tools` and `.ttl_ms`; a bare list would AttributeError."""
    assert inspect.signature(McpUpstream.list_tools).return_annotation == "ToolListResult"
    result = ToolListResult()
    assert result.tools == []
    assert result.ttl_ms is None
    # The two attributes the gateway actually consumes.
    assert hasattr(result, "tools") and hasattr(result, "ttl_ms")


def test_cache_round_trips_the_tool_list_type_the_gateway_passes() -> None:
    """`put` accepts list[dict] and `get` returns list[dict] — not `str`."""
    put_params = inspect.signature(TtlToolCache.put).parameters
    assert put_params["tools"].annotation == "list[dict[str, Any]]"
    assert put_params["server_ttl_ms"].annotation == "int | None"
    assert inspect.signature(TtlToolCache.get).return_annotation == "list[dict[str, Any]] | None"


def test_optional_server_ttl_is_treated_as_do_not_cache() -> None:
    """An upstream that declares no ttlMs must not crash the clamp (`min(None, n)`)."""
    cache = TtlToolCache(redis=object(), max_ttl_ms=60_000)  # type: ignore[arg-type]
    assert cache._effective_ttl_ms(None) == 0
    assert cache._effective_ttl_ms(0) == 0
    assert cache._effective_ttl_ms(-1) == 0
    assert cache._effective_ttl_ms(30_000) == 30_000
    assert cache._effective_ttl_ms(90_000) == 60_000


def test_default_opa_paths_name_rules_that_exist_in_the_rego_policy() -> None:
    """The queried data paths must match `package mcp.gateway` rules.

    OPA answers an undefined document with 200 and no `result` key, so a renamed
    package or rule is invisible at the transport layer. Pinning the derivation
    here makes the drift a test failure instead of an empty tool list.
    """
    from pathlib import Path

    rego = Path(__file__).resolve().parents[3] / "policies" / "mcp" / "gateway.rego"
    source = rego.read_text(encoding="utf-8")

    assert "package mcp.gateway" in source, "the policy package moved; update the data paths"
    # /v1/data/<package path>/<rule>
    assert DEFAULT_FILTER_PATH == "/v1/data/mcp/gateway/filter"
    assert DEFAULT_ALLOW_PATH == "/v1/data/mcp/gateway/allow"
    for path in (DEFAULT_FILTER_PATH, DEFAULT_ALLOW_PATH):
        rule = path.rsplit("/", 1)[-1]
        assert f"\n{rule} " in source or f"\n{rule}(" in source or f"\n{rule} :=" in source, (
            f"rule '{rule}' is queried by the backend but not defined in gateway.rego"
        )
