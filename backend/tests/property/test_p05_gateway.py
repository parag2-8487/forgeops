# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test P-05: Gateway routing is body-independent; rejected requests do zero upstream work."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, create_autospec

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from src.core.errors import ProblemException
from src.mcp.auth import OidcTokenVerifier
from src.mcp.cache import TtlToolCache
from src.mcp.gateway import McpGateway
from src.mcp.policy import OpaGatewayPolicy
from src.mcp.registry import McpServerRegistry, ServerDescriptor
from src.mcp.routing import HeaderRouter
from src.mcp.upstream import McpUpstream

# --- Strategies ---

body_strategy = st.one_of(
    st.binary(min_size=0, max_size=256),
    st.text(min_size=1, max_size=128).map(lambda t: t.encode("utf-8")),
    st.dictionaries(
        st.text(min_size=1, max_size=10),
        st.text(min_size=0, max_size=20),
        min_size=0,
        max_size=5,
    ).map(lambda d: json.dumps(d).encode("utf-8")),
)

valid_json_rpc_body_strategy = st.fixed_dictionaries(
    {
        "jsonrpc": st.just("2.0"),
        "method": st.just("tools/call"),
        "id": st.integers(min_value=1, max_value=9999),
        "params": st.fixed_dictionaries(
            {
                "name": st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz_"),
                "arguments": st.dictionaries(
                    st.text(min_size=1, max_size=10, alphabet="abcdefghijklmnop"),
                    st.text(min_size=0, max_size=20),
                    min_size=0,
                    max_size=3,
                ),
            }
        ),
    }
).map(lambda d: json.dumps(d).encode("utf-8"))

bearer_strategy = st.text(min_size=1, max_size=50, alphabet="ABCDEFabcdef0123456789.-_")


# --- Fixtures ---


def _make_gateway(
    *,
    auth_raises: Exception | None = None,
    route_raises: Exception | None = None,
    policy_denies: bool = False,
    upstream_counter: MagicMock | None = None,
):
    """Build a gateway with mocked components for property testing."""
    server = ServerDescriptor(
        name="test-server",
        url="http://fake:9999",
        capabilities=["tools/list", "tools/call"],
    )
    registry = McpServerRegistry({"test-server": server})

    # Auth
    # create_autospec(..., spec_set=True) rather than AsyncMock(spec=...): `spec=`
    # constrains attribute NAMES only, so a child called with the wrong keywords
    # still passes. That is precisely how D-23 survived 419 green tests. autospec
    # gives each child the real method's signature, and spec_set makes assigning
    # over a child raise instead of silently discarding the enforcement.
    verifier = create_autospec(OidcTokenVerifier, spec_set=True, instance=True)
    if auth_raises:
        verifier.verify.side_effect = auth_raises
    else:
        verifier.verify.return_value = MagicMock(sub="user1", iss="https://issuer", aud="gateway", raw={})

    # Router
    router = HeaderRouter(registry)

    # Policy
    policy = create_autospec(OpaGatewayPolicy, spec_set=True, instance=True)
    if policy_denies:
        policy.authorise_call.side_effect = ProblemException(
            status=403,
            type_suffix="mcp-call-denied",
            title="Tool call denied",
            detail="Policy denied.",
        )
        policy.filter_tools.return_value = []
    else:
        policy.authorise_call.return_value = None
        policy.filter_tools.return_value = [{"name": "tool1"}]

    # Cache
    cache = create_autospec(TtlToolCache, spec_set=True, instance=True)
    cache.get.return_value = None
    cache.put.return_value = True

    # Upstream
    from src.mcp.upstream import ToolListResult

    upstream = create_autospec(McpUpstream, spec_set=True, instance=True)
    counter = upstream_counter or MagicMock()
    counter.call_count = 0

    async def _list_tools(server_desc, **kw):
        counter()
        return ToolListResult(tools=[{"name": "tool1"}], ttl_ms=5000)

    async def _call_tool(server_desc, call, **kw):
        counter()
        return {"result": "ok"}

    upstream.list_tools.side_effect = _list_tools
    upstream.call_tool.side_effect = _call_tool

    gateway = McpGateway(
        registry=registry,
        verifier=verifier,
        router=router,
        policy=policy,
        cache=cache,
        upstream=upstream,
    )
    return gateway, counter


FIXED_HEADERS = {"Mcp-Method": "tools/call", "Mcp-Name": "test-server"}
LIST_HEADERS = {"Mcp-Method": "tools/list", "Mcp-Name": "test-server"}


# --- P-05a: Route is body-independent ---


@given(body1=body_strategy, body2=body_strategy)
@settings(max_examples=100)
def test_route_is_body_independent(body1, body2):
    """HeaderRouter produces the same route regardless of body content."""
    server = ServerDescriptor(
        name="test-server",
        url="http://fake:9999",
        capabilities=["tools/list", "tools/call"],
    )
    registry = McpServerRegistry({"test-server": server})
    router = HeaderRouter(registry)

    route1 = router.route(FIXED_HEADERS)
    route2 = router.route(FIXED_HEADERS)

    assert route1 == route2
    assert route1.server.name == "test-server"
    assert route1.method == "tools/call"


# --- P-05b: Invalid bearer → zero upstream ---


@given(token=bearer_strategy)
@settings(max_examples=50)
@pytest.mark.asyncio
async def test_invalid_bearer_zero_upstream(token):
    """Any invalid bearer token results in zero upstream invocations."""
    gateway, counter = _make_gateway(
        auth_raises=ProblemException(
            status=401,
            type_suffix="mcp-invalid-token",
            title="Invalid token",
            detail="Token could not be decoded.",
        )
    )

    with pytest.raises(ProblemException) as exc_info:
        await gateway.handle_tools_call(
            authorization=f"Bearer {token}",
            headers=FIXED_HEADERS,
            body=b'{"jsonrpc":"2.0","method":"tools/call","id":1,"params":{"name":"x","arguments":{}}}',
        )

    assert exc_info.value.problem.status == 401
    assert counter.call_count == 0


# --- P-05c: Missing routing headers → zero upstream ---


@given(body=body_strategy)
@settings(max_examples=50)
@pytest.mark.asyncio
async def test_missing_headers_zero_upstream(body):
    """Missing MCP routing headers → 400, zero upstream."""
    gateway, counter = _make_gateway()

    # Missing both headers
    with pytest.raises(ProblemException) as exc_info:
        await gateway.handle_tools_call(
            authorization="Bearer valid_token",
            headers={},
            body=body,
        )

    assert exc_info.value.problem.status == 400
    assert counter.call_count == 0


# --- P-05d: Unknown server → zero upstream ---


@given(server_name=st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnop"))
@settings(max_examples=50)
@pytest.mark.asyncio
async def test_unknown_server_zero_upstream(server_name):
    """Routing to an unknown server → 404, zero upstream."""
    assume(server_name != "test-server")
    gateway, counter = _make_gateway()

    headers = {"Mcp-Method": "tools/call", "Mcp-Name": server_name}
    with pytest.raises(ProblemException) as exc_info:
        await gateway.handle_tools_call(
            authorization="Bearer valid_token",
            headers=headers,
            body=b'{"jsonrpc":"2.0","method":"tools/call","id":1,"params":{"name":"x","arguments":{}}}',
        )

    assert exc_info.value.problem.status == 404
    assert counter.call_count == 0


# --- P-05e: OPA deny → zero upstream ---


@given(body=valid_json_rpc_body_strategy)
@settings(max_examples=50)
@pytest.mark.asyncio
async def test_opa_deny_zero_upstream(body):
    """OPA policy denial → 403, zero upstream invocation."""
    gateway, counter = _make_gateway(policy_denies=True)

    with pytest.raises(ProblemException) as exc_info:
        await gateway.handle_tools_call(
            authorization="Bearer valid_token",
            headers=FIXED_HEADERS,
            body=body,
        )

    assert exc_info.value.problem.status == 403
    assert counter.call_count == 0


# --- P-05f: Malformed body → zero upstream ---


@given(
    body=st.sampled_from(
        [
            b"not json at all",
            b"<xml>bad</xml>",
            b"\xff\xfe\x00",
            b"{{{{",
            b"",
            b"\x00\x01\x02",
            b"true",
            b"null",
            b"12345",
            b"[1,2,3]",
        ]
    )
)
@settings(max_examples=50)
@pytest.mark.asyncio
async def test_malformed_body_zero_upstream(body):
    """Malformed or non-object JSON body → 400, zero upstream."""
    gateway, counter = _make_gateway()

    with pytest.raises(ProblemException) as exc_info:
        await gateway.handle_tools_call(
            authorization="Bearer valid_token",
            headers=FIXED_HEADERS,
            body=body,
        )

    assert exc_info.value.problem.status == 400
    assert counter.call_count == 0


# --- P-05g: Allowed call → exactly one upstream dispatch ---


@given(body=valid_json_rpc_body_strategy)
@settings(max_examples=50)
@pytest.mark.asyncio
async def test_allowed_call_exactly_one_upstream(body):
    """An allowed tools/call invokes upstream exactly once."""
    gateway, counter = _make_gateway()

    result = await gateway.handle_tools_call(
        authorization="Bearer valid_token",
        headers=FIXED_HEADERS,
        body=body,
    )

    assert result == {"result": "ok"}
    assert counter.call_count == 1
