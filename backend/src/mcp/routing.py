# SPDX-License-Identifier: FSL-1.1-ALv2
"""Header-based MCP routing. The body is NEVER parsed here (P-05)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ..core.errors import ProblemException
from .registry import McpServerRegistry, ServerDescriptor

MCP_METHOD_HEADER = "Mcp-Method"
MCP_NAME_HEADER = "Mcp-Name"


@dataclass(frozen=True)
class Route:
    """Resolved MCP route from header inspection."""

    server: ServerDescriptor
    method: str
    kind: Literal["tools_list", "tools_call", "tasks", "other"]


def _classify(method: str) -> Literal["tools_list", "tools_call", "tasks", "other"]:
    if method == "tools/list":
        return "tools_list"
    if method == "tools/call":
        return "tools_call"
    if method.startswith("tasks/"):
        return "tasks"
    return "other"


class HeaderRouter:
    """Routes purely from headers. The JSON-RPC body is NEVER parsed here.

    This is the point of the stateless gateway spec and the property that
    makes routing O(1) and body-independent (P-05).
    """

    def __init__(self, registry: McpServerRegistry) -> None:
        self._registry = registry

    def route(self, headers: Mapping[str, str]) -> Route:
        method = (headers.get(MCP_METHOD_HEADER) or "").strip()
        name = (headers.get(MCP_NAME_HEADER) or "").strip()

        if not method or not name:
            raise ProblemException(
                status=400,
                type_suffix="mcp-missing-routing-headers",
                title="Missing MCP routing headers",
                detail=f"Both {MCP_METHOD_HEADER} and {MCP_NAME_HEADER} are required.",
            )

        server = self._registry.get(name)
        if server is None:
            raise ProblemException(
                status=404,
                type_suffix="mcp-unknown-server",
                title="Unknown MCP server",
                detail=f"No MCP server named '{name}'.",
            )

        return Route(server=server, method=method, kind=_classify(method))
