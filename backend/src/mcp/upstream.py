# SPDX-License-Identifier: FSL-1.1-ALv2
"""Upstream MCP server transport with W3C trace propagation (Design §11.4).

The gateway hands this class the routed `ServerDescriptor` and, for a call, a
parsed `ToolCall`. It does not hand over a bare URL string: routing already
resolved the descriptor, and passing the descriptor keeps the URL-building rule
in one place.

`list_tools` returns a `ToolListResult` rather than a bare list because the
server-declared `ttlMs` travels with the tool list and the cache needs both. An
earlier revision returned a bare list while the gateway called `.get("tools")` on
it — an `AttributeError` on every cache miss that no test caught, because the
gateway was only ever composed with fakes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.errors import ProblemException
from .registry import ServerDescriptor

logger = logging.getLogger(__name__)

# W3C Trace Context headers to propagate
_TRACE_HEADERS = ("traceparent", "tracestate")


@dataclass(frozen=True)
class ToolCall:
    """A parsed tools/call request: the tool name and its arguments."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolListResult:
    """An upstream tools/list response plus its server-declared TTL."""

    tools: list[dict[str, Any]] = field(default_factory=list)
    ttl_ms: int | None = None


class McpUpstream:
    """Forwards MCP requests to upstream servers with trace propagation."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._http = http or httpx.AsyncClient(timeout=timeout)

    async def list_tools(
        self,
        server: ServerDescriptor,
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> ToolListResult:
        """Forward a tools/list request and return the tools plus any ttlMs."""
        body = await self._post(
            server,
            {"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            what="tools/list",
            trace_headers=trace_headers,
        )
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            return ToolListResult()

        tools = result.get("tools")
        ttl_ms = result.get("ttlMs", result.get("ttl_ms"))
        return ToolListResult(
            tools=tools if isinstance(tools, list) else [],
            ttl_ms=ttl_ms if isinstance(ttl_ms, int) else None,
        )

    async def call_tool(
        self,
        server: ServerDescriptor,
        call: ToolCall,
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Forward a tools/call request. This is the sole dispatch site (P-05)."""
        body = await self._post(
            server,
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": call.tool, "arguments": call.arguments},
            },
            what=f"tools/call '{call.tool}'",
            trace_headers=trace_headers,
        )
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else {}

    # ── Transport ─────────────────────────────────────────────────────────────

    async def _post(
        self,
        server: ServerDescriptor,
        payload: dict[str, Any],
        *,
        what: str,
        trace_headers: dict[str, str] | None,
    ) -> Any:
        headers = self._build_headers(trace_headers)
        headers["Content-Type"] = "application/json"
        url = f"{server.url.rstrip('/')}/mcp"

        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("upstream %s failed: %s", what, exc)
            raise ProblemException(
                status=502,
                type_suffix="mcp-upstream-error",
                title="Upstream MCP server error",
                detail=f"{what} to server '{server.name}' returned {exc.response.status_code}.",
            ) from exc
        except ProblemException:
            raise
        except Exception as exc:
            logger.error("upstream %s transport error: %s", what, exc)
            raise ProblemException(
                status=502,
                type_suffix="mcp-upstream-unreachable",
                title="Upstream MCP server unreachable",
                detail=f"Could not reach upstream server '{server.name}'.",
            ) from exc

    def _build_headers(self, trace_headers: dict[str, str] | None) -> dict[str, str]:
        """Build request headers with trace propagation."""
        headers: dict[str, str] = {}
        if trace_headers:
            for h in _TRACE_HEADERS:
                if h in trace_headers:
                    headers[h] = trace_headers[h]
        return headers
