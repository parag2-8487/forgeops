# SPDX-License-Identifier: FSL-1.1-ALv2
"""MCP Gateway orchestrator — composes auth, routing, cache, policy, upstream."""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import ProblemException
from .auth import OidcTokenVerifier
from .cache import TtlToolCache
from .policy import OpaGatewayPolicy
from .registry import McpServerRegistry, ServerDescriptor
from .routing import HeaderRouter
from .upstream import McpUpstream, ToolCall


class McpGateway:
    """Stateless MCP gateway orchestrator."""

    def __init__(
        self,
        *,
        registry: McpServerRegistry,
        verifier: OidcTokenVerifier,
        router: HeaderRouter,
        policy: OpaGatewayPolicy,
        cache: TtlToolCache,
        upstream: McpUpstream,
        agent_blast_radius: str = "read_only",
    ) -> None:
        self._registry = registry
        self._verifier = verifier
        self._router = router
        self._policy = policy
        self._cache = cache
        self._upstream = upstream
        self._agent_blast_radius = agent_blast_radius

    async def handle_tools_list(self, *, authorization: str | None, headers: dict[str, str]) -> dict[str, Any]:
        """tools/list: authenticate → route → cache/upstream → OPA filter → return."""
        claims = await self._verifier.verify(authorization)
        route = self._router.route(headers)

        # Cache or upstream
        tools = await self._cache.get(route.server.name)
        if tools is None:
            upstream_result = await self._upstream.list_tools(route.server)
            tools = upstream_result.tools
            await self._cache.put(route.server.name, tools, upstream_result.ttl_ms)

        # OPA filter on EVERY response (including cache hits)
        allowed = await self._policy.filter_tools(
            server=route.server.name,
            tools=tools,
            claims=claims,
            blast_radius=self._agent_blast_radius,
        )

        return {"tools": allowed}

    async def handle_tools_call(
        self, *, authorization: str | None, headers: dict[str, str], body: bytes
    ) -> dict[str, Any]:
        """tools/call: authenticate → route → parse tool → resolve metadata → OPA authorize → invoke."""
        claims = await self._verifier.verify(authorization)
        route = self._router.route(headers)  # body-independent

        # Parse called tool AFTER route is fixed
        call = self._parse_tools_call(body)

        # Resolve metadata locally (no upstream I/O)
        metadata = await self._resolve_metadata(route.server, call.tool)

        # OPA authorize (deny = return before dispatch)
        await self._policy.authorise_call(
            server=route.server.name,
            tool=call.tool,
            metadata=metadata,
            claims=claims,
            blast_radius=self._agent_blast_radius,
        )

        # ONLY on allow: invoke upstream
        return await self._upstream.call_tool(route.server, call)

    def _parse_tools_call(self, body: bytes) -> ToolCall:
        """Parse a tools/call JSON-RPC body into {tool, arguments}.

        Raises 400 on malformed body.
        """
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ProblemException(
                status=400,
                type_suffix="mcp-invalid-body",
                title="Invalid request body",
                detail="Request body is not valid JSON.",
            ) from e

        if not isinstance(payload, dict):
            raise ProblemException(
                status=400,
                type_suffix="mcp-invalid-body",
                title="Invalid request body",
                detail="Request body must be a JSON object.",
            )

        params = payload.get("params", {})
        tool_name = params.get("name")
        if not tool_name or not isinstance(tool_name, str):
            raise ProblemException(
                status=400,
                type_suffix="mcp-missing-tool-name",
                title="Missing tool name",
                detail="params.name is required in tools/call.",
            )

        return ToolCall(tool=tool_name, arguments=params.get("arguments") or {})

    async def _resolve_metadata(self, server: ServerDescriptor, tool_name: str) -> dict[str, Any]:
        """Resolve tool metadata without any upstream I/O (design §11.4, P-05).

        The only sources are the server's configured capabilities and an
        already-valid Redis cache entry from a previous ``tools/list``. When the
        tool's descriptor is not known, no annotation is attached, and the Rego
        `allow` rule defaults it to the ``infrastructure`` blast radius — the
        highest — so an unknown tool is denied to anything less privileged rather
        than being waved through or silently dispatched.
        """
        # Check that the server has capabilities for tool calls
        if "tools/call" not in server.capabilities:
            raise ProblemException(
                status=404,
                type_suffix="mcp-tool-not-found",
                title="Tool not found",
                detail=f"Server '{server.name}' does not support tools/call.",
            )

        metadata: dict[str, Any] = {
            "server": server.name,
            "tool": tool_name,
            "server_url": server.url,
        }

        cached = await self._cache.get(server.name)
        descriptor = _find_tool(cached, tool_name)
        if descriptor is not None:
            metadata["tool_descriptor"] = descriptor
            annotations = descriptor.get("annotations")
            if isinstance(annotations, dict):
                metadata["annotations"] = annotations
        return metadata

    async def aclose(self) -> None:
        """Clean up resources."""
        pass


def _find_tool(tools: list[dict[str, Any]] | None, tool_name: str) -> dict[str, Any] | None:
    """Locate a tool descriptor by name in a cached tools/list payload."""
    if not tools:
        return None
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name") == tool_name:
            return tool
    return None
