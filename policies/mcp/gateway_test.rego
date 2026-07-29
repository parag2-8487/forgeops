# policies/mcp/gateway_test.rego — Unit tests for the MCP Gateway policy.
#
# Covers:
#   - read_only agent: sees only read_only tools
#   - workspace agent: sees read_only + workspace tools
#   - infrastructure agent: sees all tools
#   - unknown/missing annotations default to infrastructure (highest risk)
#   - absent tools result in empty filter and deny
#   - deny-by-default when no tools match
#
# Design: §5.3–§5.4, §11.4, §17.2 OQ-20
package mcp.gateway_test

import rego.v1

import data.mcp.gateway

# ─── Test fixtures ───────────────────────────────────────────────────────────

tool_read_only := {
	"name": "agent.health",
	"annotations": {"blast_radius": "read_only"},
}

tool_workspace := {
	"name": "agent.file.write",
	"annotations": {"blast_radius": "workspace"},
}

tool_infrastructure := {
	"name": "agent.deploy",
	"annotations": {"blast_radius": "infrastructure"},
}

# Tool with no annotations at all (missing annotations field)
tool_no_annotations := {"name": "unknown.tool"}

# Tool with annotations but no blast_radius field
tool_missing_radius := {
	"name": "partial.tool",
	"annotations": {"description": "some tool"},
}

all_tools := [tool_read_only, tool_workspace, tool_infrastructure]

# ─── radius_rank tests ───────────────────────────────────────────────────────

test_radius_rank_ordering if {
	gateway.radius_rank["read_only"] < gateway.radius_rank["workspace"]
	gateway.radius_rank["workspace"] < gateway.radius_rank["infrastructure"]
}

# ─── tool_radius tests ───────────────────────────────────────────────────────

test_tool_radius_annotated_read_only if {
	gateway.tool_radius(tool_read_only) == "read_only"
}

test_tool_radius_annotated_workspace if {
	gateway.tool_radius(tool_workspace) == "workspace"
}

test_tool_radius_annotated_infrastructure if {
	gateway.tool_radius(tool_infrastructure) == "infrastructure"
}

# Unknown tools default to infrastructure (highest risk)
test_tool_radius_no_annotations_defaults_to_infrastructure if {
	gateway.tool_radius(tool_no_annotations) == "infrastructure"
}

test_tool_radius_missing_blast_radius_defaults_to_infrastructure if {
	gateway.tool_radius(tool_missing_radius) == "infrastructure"
}

# ─── filter rule tests — read_only agent ─────────────────────────────────────

test_filter_read_only_agent_sees_only_read_only if {
	result := gateway.filter with input as {
		"tools": all_tools,
		"agent_blast_radius": "read_only",
	}
	count(result) == 1
	result[0].name == "agent.health"
}

# ─── filter rule tests — workspace agent ─────────────────────────────────────

test_filter_workspace_agent_sees_read_only_and_workspace if {
	result := gateway.filter with input as {
		"tools": all_tools,
		"agent_blast_radius": "workspace",
	}
	count(result) == 2
	names := {t.name | some t in result}
	names == {"agent.health", "agent.file.write"}
}

# ─── filter rule tests — infrastructure agent ────────────────────────────────

test_filter_infrastructure_agent_sees_all if {
	result := gateway.filter with input as {
		"tools": all_tools,
		"agent_blast_radius": "infrastructure",
	}
	count(result) == 3
}

# ─── filter rule tests — unknown annotations ─────────────────────────────────

# Unknown tools default to infrastructure, so only infrastructure agents see them
test_filter_unknown_tool_not_visible_to_read_only if {
	result := gateway.filter with input as {
		"tools": [tool_no_annotations],
		"agent_blast_radius": "read_only",
	}
	count(result) == 0
}

test_filter_unknown_tool_not_visible_to_workspace if {
	result := gateway.filter with input as {
		"tools": [tool_no_annotations],
		"agent_blast_radius": "workspace",
	}
	count(result) == 0
}

test_filter_unknown_tool_visible_to_infrastructure if {
	result := gateway.filter with input as {
		"tools": [tool_no_annotations],
		"agent_blast_radius": "infrastructure",
	}
	count(result) == 1
}

test_filter_missing_radius_not_visible_to_workspace if {
	result := gateway.filter with input as {
		"tools": [tool_missing_radius],
		"agent_blast_radius": "workspace",
	}
	count(result) == 0
}

# ─── filter rule tests — absent/empty tools ──────────────────────────────────

test_filter_empty_tools_returns_empty if {
	result := gateway.filter with input as {
		"tools": [],
		"agent_blast_radius": "infrastructure",
	}
	count(result) == 0
}

# ─── allow rule tests — read_only agent ──────────────────────────────────────

test_allow_read_only_agent_can_call_read_only_tool if {
	gateway.allow with input as {
		"tools": all_tools,
		"tool": "agent.health",
		"agent_blast_radius": "read_only",
	}
}

test_allow_read_only_agent_denied_workspace_tool if {
	not gateway.allow with input as {
		"tools": all_tools,
		"tool": "agent.file.write",
		"agent_blast_radius": "read_only",
	}
}

test_allow_read_only_agent_denied_infrastructure_tool if {
	not gateway.allow with input as {
		"tools": all_tools,
		"tool": "agent.deploy",
		"agent_blast_radius": "read_only",
	}
}

# ─── allow rule tests — workspace agent ──────────────────────────────────────

test_allow_workspace_agent_can_call_read_only_tool if {
	gateway.allow with input as {
		"tools": all_tools,
		"tool": "agent.health",
		"agent_blast_radius": "workspace",
	}
}

test_allow_workspace_agent_can_call_workspace_tool if {
	gateway.allow with input as {
		"tools": all_tools,
		"tool": "agent.file.write",
		"agent_blast_radius": "workspace",
	}
}

test_allow_workspace_agent_denied_infrastructure_tool if {
	not gateway.allow with input as {
		"tools": all_tools,
		"tool": "agent.deploy",
		"agent_blast_radius": "workspace",
	}
}

# ─── allow rule tests — infrastructure agent ─────────────────────────────────

test_allow_infrastructure_agent_can_call_all if {
	gateway.allow with input as {
		"tools": all_tools,
		"tool": "agent.health",
		"agent_blast_radius": "infrastructure",
	}
	gateway.allow with input as {
		"tools": all_tools,
		"tool": "agent.file.write",
		"agent_blast_radius": "infrastructure",
	}
	gateway.allow with input as {
		"tools": all_tools,
		"tool": "agent.deploy",
		"agent_blast_radius": "infrastructure",
	}
}

# ─── allow rule tests — unknown/missing annotations ──────────────────────────

# Unknown tool defaults to infrastructure, so read_only agent is denied
test_allow_unknown_tool_denied_for_read_only if {
	not gateway.allow with input as {
		"tools": [tool_no_annotations],
		"tool": "unknown.tool",
		"agent_blast_radius": "read_only",
	}
}

# Unknown tool defaults to infrastructure, so workspace agent is denied
test_allow_unknown_tool_denied_for_workspace if {
	not gateway.allow with input as {
		"tools": [tool_no_annotations],
		"tool": "unknown.tool",
		"agent_blast_radius": "workspace",
	}
}

# Unknown tool defaults to infrastructure, so only infrastructure agent is allowed
test_allow_unknown_tool_allowed_for_infrastructure if {
	gateway.allow with input as {
		"tools": [tool_no_annotations],
		"tool": "unknown.tool",
		"agent_blast_radius": "infrastructure",
	}
}

# ─── allow rule tests — absent tools (deny by default) ───────────────────────

test_allow_denied_when_tool_not_in_list if {
	not gateway.allow with input as {
		"tools": all_tools,
		"tool": "nonexistent.tool",
		"agent_blast_radius": "infrastructure",
	}
}

test_allow_denied_when_tools_empty if {
	not gateway.allow with input as {
		"tools": [],
		"tool": "agent.health",
		"agent_blast_radius": "infrastructure",
	}
}

# ─── deny-by-default behavior ────────────────────────────────────────────────

# If no input matches, allow should not be true
test_deny_by_default_no_matching_tool if {
	not gateway.allow with input as {
		"tools": [tool_infrastructure],
		"tool": "agent.health",
		"agent_blast_radius": "infrastructure",
	}
}
