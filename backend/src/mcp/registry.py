# SPDX-License-Identifier: FSL-1.1-ALv2
"""MCP server registry and descriptors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServerDescriptor:
    """Describes a registered MCP server."""

    name: str
    url: str
    description: str = ""
    capabilities: list[str] = field(default_factory=list)


class McpServerRegistry:
    """Immutable registry of MCP servers, loaded from configuration."""

    def __init__(self, servers: dict[str, ServerDescriptor]) -> None:
        self._servers = dict(servers)

    def get(self, name: str) -> ServerDescriptor | None:
        return self._servers.get(name)

    def all(self) -> dict[str, ServerDescriptor]:
        return dict(self._servers)

    @classmethod
    def from_config(cls, config: list[dict[str, Any]]) -> McpServerRegistry:
        """Build registry from a list of server config dicts."""
        servers = {}
        for entry in config:
            name = entry["name"]
            servers[name] = ServerDescriptor(
                name=name,
                url=entry["url"],
                description=entry.get("description", ""),
                capabilities=entry.get("capabilities", []),
            )
        return cls(servers)
