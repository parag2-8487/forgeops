# SPDX-License-Identifier: FSL-1.1-ALv2
"""Python MCP server template with schema-validated dispatch (Design §12.6).

Provides a ToolSpec dataclass and a dispatcher that validates input against the
tool's JSON Schema before calling the handler.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import ProblemException

logger = logging.getLogger(__name__)

# Type alias for async tool handlers
ToolHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


def _validate_json_schema(instance: Any, schema: dict[str, Any]) -> str | None:
    """Minimal JSON Schema validator for tool input.

    Returns an error message string on validation failure, or None if valid.
    Supports: type, properties, required, additionalProperties.
    """
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(instance, dict):
            return f"Expected object, got {type(instance).__name__}"

        # Check required
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                return f"Missing required property: '{key}'"

        # Check properties
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                prop_schema = properties[key]
                err = _validate_json_schema(value, prop_schema)
                if err:
                    return f"Property '{key}': {err}"

        # Check additionalProperties
        additional = schema.get("additionalProperties", True)
        if additional is False:
            extra = set(instance.keys()) - set(properties.keys())
            if extra:
                return f"Additional properties not allowed: {sorted(extra)}"

    elif schema_type == "string":
        if not isinstance(instance, str):
            return f"Expected string, got {type(instance).__name__}"
    elif schema_type == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            return f"Expected integer, got {type(instance).__name__}"
    elif schema_type == "number":
        if not isinstance(instance, int | float) or isinstance(instance, bool):
            return f"Expected number, got {type(instance).__name__}"
    elif schema_type == "boolean":
        if not isinstance(instance, bool):
            return f"Expected boolean, got {type(instance).__name__}"
    elif schema_type == "array":
        if not isinstance(instance, list):
            return f"Expected array, got {type(instance).__name__}"

    return None


@dataclass(frozen=True)
class ToolSpec:
    """Specification for a single MCP tool.

    Attributes:
        name: Dot-separated tool name (e.g., 'platform.health').
        description: Human-readable description.
        input_schema: JSON Schema dict for validating input arguments.
        blast_radius: Severity hint ('none', 'low', 'medium', 'high').
        handler: Async function that processes the tool call.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    blast_radius: str = "none"
    handler: ToolHandler = field(repr=False, compare=False, default=None)  # type: ignore[assignment]


async def _platform_health_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    """Built-in platform health tool handler."""
    return {
        "status": "healthy",
        "service": "forgeops-backend",
        "version": "0.0.0",
    }


# Built-in platform.health tool
PLATFORM_HEALTH_SPEC = ToolSpec(
    name="platform.health",
    description="Returns the health status of the ForgeOps platform.",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    blast_radius="none",
    handler=_platform_health_handler,
)


class McpServerDispatcher:
    """Dispatcher that validates input against tool schema and calls the handler.

    Registered tools are served via list_tools() and dispatched via call_tool().
    """

    def __init__(self, tools: list[ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        initial = tools if tools is not None else [PLATFORM_HEALTH_SPEC]
        for spec in initial:
            self._tools[spec.name] = spec

    def register(self, spec: ToolSpec) -> None:
        """Register a tool spec."""
        self._tools[spec.name] = spec

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all registered tools as MCP tool descriptors."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
                "annotations": {
                    "blastRadius": spec.blast_radius,
                },
            }
            for spec in self._tools.values()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate input and dispatch to the tool handler.

        Raises ProblemException (400) on unknown tool or schema violation.
        """
        spec = self._tools.get(name)
        if spec is None:
            raise ProblemException(
                status=400,
                type_suffix="mcp-unknown-tool",
                title="Unknown tool",
                detail=f"No tool named '{name}' is registered.",
            )

        args = arguments or {}

        # Validate input against schema
        error = _validate_json_schema(args, spec.input_schema)
        if error:
            raise ProblemException(
                status=400,
                type_suffix="mcp-invalid-input",
                title="Invalid tool input",
                detail=f"Input validation failed for tool '{name}': {error}",
            )

        if spec.handler is None:
            raise ProblemException(
                status=500,
                type_suffix="mcp-no-handler",
                title="Tool has no handler",
                detail=f"Tool '{name}' is registered but has no handler.",
            )

        return await spec.handler(args)
