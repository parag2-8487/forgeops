# policies/agent/paths.rego — phases.md §1.7's "Never edit package.json".
#
# Design: §11.7; Deliverable 1.7; criterion 7.
#
# DATA-DRIVEN, NOT LITERAL
# `package.json` appears nowhere in this file either. The globs arrive on
# `input.project.protected_globs`, so the same rule protects a Go project's `go.mod` and a
# Python project's `pyproject.toml` without a second policy.
#
# THIS IS NOT THE AGENT'S WRITE BLOCKLIST, AND THE DIFFERENCE MATTERS
# `agent/internal/executor/internal/mutate/blocklist.go` refuses `.env`, `*.pem` and the
# rest unconditionally, inside the mutation chokepoint, and no policy can switch it off
# (Q-01). This is the governed, per-project layer above it: a project decides that
# `package.json` is off limits, and that decision is data an operator can change. A path
# the blocklist refuses is refused whatever this file says.
#
# `default allow := false` for the D-25 reason `governance.rego` states at length.
package forgeops.governance.paths

import rego.v1

default allow := false

allow if deny_reason == ""

default deny_reason := ""

# `sort` and `concat` rather than `sprintf("%v", [set])`: a set's printed form is stable
# in OPA today, but Q-06 compares this string across two evaluators and §11.7 surfaces it
# to a user, so the ordering is made explicit here instead of inherited from a formatter.
deny_reason := sprintf("protected path: %s", [concat(", ", sort(violations))]) if {
	count(violations) > 0
}

violations contains item.file_path if {
	some item in input.change_items
	some pattern in protected_globs
	matches(pattern, item.file_path)
}

# `glob.match("**/package.json", ["/"], "package.json")` is FALSE (finding 69): with `/`
# as the delimiter, the `**/` prefix needs at least one leading segment to consume. So the
# single most natural way to write "package.json anywhere" is exactly the way that misses
# the `package.json` at the repository root — which is the file phases.md §1.7 names.
# Rather than require every project to write the pattern twice and rely on nobody
# forgetting, a `**/` prefix means "at the root or below" here.
#
# The widening is scoped to that exact prefix and nothing else: `*/package.json` still does
# not match a root-level file, and `test_a_single_star_does_not_span_path_separators` pins
# that, so this is one documented convenience rather than a general loosening of glob
# semantics that would make a project's stated pattern mean something else.
matches(pattern, path) if glob.match(pattern, ["/"], path)

matches(pattern, path) if {
	startswith(pattern, "**/")
	glob.match(trim_prefix(pattern, "**/"), ["/"], path)
}

protected_globs := {pattern | some pattern in input.project.protected_globs}
