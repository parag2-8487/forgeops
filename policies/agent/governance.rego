# policies/agent/governance.rego — the governance bundle's ENTRY DOCUMENT.
#
# Design: §8.3 (the `policy` CI job), §11.7 (the three named policies), §13.4
# (`make policy-test`); phases.md §1.7; Deliverable 1.7; criterion 7.
#
# WHAT THIS DOCUMENT IS FOR
# The backend's governance client and the agent's embedded evaluator both query
# `data.forgeops.governance.decision` and must get the same answer for the same input
# (Q-06). Everything else in this bundle is a sub-policy this file composes; nothing
# outside the bundle should query a sub-policy directly, because precedence between
# them — schedule before paths before approval — is decided HERE and nowhere else.
#
# WHY `default allow := false` IS LOAD-BEARING, NOT DECORATION (D-25)
# An OPA server asked for an UNDEFINED document answers HTTP 200 with no `result`
# key, and that is byte-identical to the answer for a bundle that was renamed, failed
# to load, or was never published. Phase 0 walked into exactly that trap at the MCP
# gateway; `policies/mcp/gateway.rego` carries the same comment and the same default
# for the same reason. So a deny here is a DEFINED `false` / a DEFINED
# `result == "deny"`, and an undefined document is a loud `governance-policy-undefined`
# (503) on the backend side and `ErrNoBundle` -> Deny on the agent side. Every one of
# the four files in this bundle carries an explicit total default at its own entry
# document, so no sub-policy can reintroduce the trap by being partial.
#
# TOTALITY IS THE INVARIANT
# `result`, `reason` and `rule` are each total, which is what makes `decision` total
# without a default of its own. `policies/agent/governance_test.rego` asserts that
# over a deliberately EMPTY input, because "total" means total for garbage too.
package forgeops.governance

import rego.v1

import data.forgeops.governance.approval
import data.forgeops.governance.paths
import data.forgeops.governance.schedule

# The one document both evaluators query. §10.6's `Decision` struct has exactly these
# three fields — Result, Reason, Rule — so the wire shape and the Go type match and
# FR-37's "surface the rule id and the human-readable reason" needs no second mapping.
decision := {
	"result": result,
	"reason": reason,
	"rule": rule,
}

# Blocking reasons, in the fixed precedence order this file owns. An array rather than
# a set so the order is the precedence order rather than OPA's iteration order: with a
# set, `reason` for an input that trips both schedule and paths would depend on string
# collation, and Q-06 compares reasons across two evaluators.
deny_reasons := [r |
	some r in [input_error, schedule.deny_reason, paths.deny_reason]
	r != ""
]

# A decision request with no operation is not a permissive request, it is a malformed one,
# and it must not be answered with anything a caller could act on. Without this clause an
# empty input reaches `approval`, comes back `require_approval` because the blast-radius
# verdict is absent, and the chokepoint is invited to open an approval flow for an
# operation nobody named. `operation` is the one field with no defensible default: the
# timezone can fall back to UTC and the glob list to empty, but "which operation" cannot.
default input_error := ""

input_error := "governance input is malformed: input.operation must be a string" if not named_operation

# Written as `not named_operation` rather than `not is_string(input.operation)` because of
# a Rego rewriting rule that is easy to get wrong in exactly the fail-open direction
# (finding 70): OPA compiles `not is_string(input.operation)` to
# `__local = input.operation; not is_string(__local)`, and the ASSIGNMENT is undefined when
# the field is absent — so the whole body fails and the guard does not fire for the one
# input it exists to catch, an input with no `operation` at all. Hoisting the positive test
# into its own rule makes the absence a failed body there and a satisfied `not` here.
named_operation if is_string(input.operation)

# `default result := "deny"` is the fail-closed direction and it does real work: it is
# the answer whenever anything blocks, AND the answer when the input is so malformed
# that neither of the two clauses below can be evaluated at all.
default result := "deny"

result := "require_approval" if {
	count(deny_reasons) == 0
	approval.require_approval
}

result := "allow" if {
	count(deny_reasons) == 0
	not approval.require_approval
}

# Kept beside `result` because §10.6's Evaluator reports both, and because a caller
# that only wants the boolean should not have to compare strings.
default allow := false

allow if result == "allow"

default reason := ""

reason := concat("; ", deny_reasons) if count(deny_reasons) > 0

reason := concat("; ", sort(approval.reasons)) if {
	count(deny_reasons) == 0
	approval.require_approval
}

# Which rule decided, for explainability (FR-37). The four bodies are mutually
# exclusive by construction, which is what a complete rule requires: two of them
# holding at once with different values is an eval-time conflict error, not a
# silently-picked winner.
default rule := "governance.default_deny"

rule := "governance.malformed_input" if input_error != ""

rule := "schedule.blocked_window" if {
	input_error == ""
	schedule.deny_reason != ""
}

rule := "paths.protected_path" if {
	input_error == ""
	schedule.deny_reason == ""
	paths.deny_reason != ""
}

rule := "approval.required" if {
	count(deny_reasons) == 0
	approval.require_approval
}

rule := "governance.allow" if {
	count(deny_reasons) == 0
	not approval.require_approval
}
