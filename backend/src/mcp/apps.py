# SPDX-License-Identifier: FSL-1.1-ALv2
"""MCP Apps sandbox hosting (Design §12.5).

App descriptors, CSP headers, and sandbox token definitions for hosted MCP apps.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

# Content Security Policy for sandboxed MCP apps.
# frame-ancestors is 'self' because the ForgeOps host page legitimately frames
# the app; 'none' would forbid the very embedding this module exists to support.
CSP_POLICY = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'self'"
)

# Sandbox attributes for the app iframe.
#
# `allow-same-origin` is DELIBERATELY ABSENT (Design §11.6). Granting it would
# put the framed app back into the parent's origin, handing it the parent's
# cookies, localStorage and same-origin fetch credentials — which defeats the
# entire point of sandboxing a third-party app UI. Scripts and forms are allowed
# because an app that can render but not run is useless; origin access is not.
SANDBOX_ATTRS = "allow-scripts allow-forms"


def generate_sandbox_token() -> str:
    """Generate a cryptographically secure sandbox token."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class McpAppDescriptor:
    """Describes a registered MCP app.

    Attributes:
        app_id: Unique identifier for the app.
        name: Human-readable name.
        description: What the app does.
        tool_name: The MCP tool this app fronts.
        entry_url: URL to the app's entry point (relative or absolute).
        permissions: List of permissions the app requires.
        csp: Content-Security-Policy header value.
        sandbox: Sandbox attribute string for iframe hosting.
    """

    app_id: str
    name: str
    description: str
    tool_name: str
    entry_url: str = ""
    permissions: list[str] = field(default_factory=list)
    csp: str = CSP_POLICY
    sandbox: str = SANDBOX_ATTRS

    @property
    def title(self) -> str:
        """Human-readable title used by the iframe host page."""
        return self.name

    def to_dict(self) -> dict[str, Any]:
        """Serialise the descriptor for the GET /api/v1/mcp/apps/{name} response."""
        return {
            "name": self.app_id,
            "title": self.name,
            "description": self.description,
            "tool_name": self.tool_name,
            "entry_url": self.entry_url,
            "capabilities": list(self.permissions),
            "csp": self.csp,
            "sandbox": self.sandbox,
        }


# Built-in descriptor for the agent health tool
AGENT_HEALTH_DESCRIPTOR = McpAppDescriptor(
    app_id="agent-health",
    name="Agent Health",
    description="Displays the health status of the local ForgeOps agent.",
    tool_name="agent.health",
    entry_url="/apps/agent-health",
    permissions=["read:agent:status"],
)


class McpAppRegistry:
    """Registry of MCP app descriptors.

    Provides lookup and listing of available MCP apps.
    """

    def __init__(self, apps: list[McpAppDescriptor] | None = None) -> None:
        self._apps: dict[str, McpAppDescriptor] = {}
        initial = apps if apps is not None else [AGENT_HEALTH_DESCRIPTOR]
        for app in initial:
            self._apps[app.app_id] = app

    def get(self, app_id: str) -> McpAppDescriptor | None:
        """Retrieve an app descriptor by ID."""
        return self._apps.get(app_id)

    def list_apps(self) -> list[McpAppDescriptor]:
        """Return all registered app descriptors."""
        return list(self._apps.values())

    def register(self, descriptor: McpAppDescriptor) -> None:
        """Register a new app descriptor."""
        self._apps[descriptor.app_id] = descriptor

    def get_sandbox_headers(self, app_id: str) -> dict[str, str]:
        """Get security headers for hosting an app in a sandbox."""
        app = self._apps.get(app_id)
        if app is None:
            return {}
        return {
            "Content-Security-Policy": app.csp,
            "X-Frame-Options": "DENY",
            "X-Sandbox-Token": generate_sandbox_token(),
        }
