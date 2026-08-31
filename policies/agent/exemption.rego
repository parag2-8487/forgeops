# policies/agent/exemption.rego — FR-34's "auto-approve documentation-only changes".
#
# Design: §11.7; phases.md §1.7. Landed with the Phase 1 P0 pass.
#
# WHAT WAS MISSING
# `auto_approve_readme_only` has been a validated key in `PROJECT_SETTINGS_KEYS` since revision `0009`,
# and `test_0009_projects.py` asserts a project can set it. Nothing read it. It was not in
# `PROJECT_PARAMETER_KEYS`, so it never reached `input.project`, and no rule in this bundle mentioned it —
# so an operator could turn it on, see it persisted, and have it change nothing. That is FR-34, and it was
# a setting rather than a feature.
#
# WHY THIS IS A SEPARATE FILE AND NOT AN EDIT TO `approval.rego`
# Two reasons, both load-bearing.
#
# First, Q-06's negative control is worded against a specific line — "invert the comparison in
# `approval.rego`'s `require_approval if input.environment == "prod"` clause to `!=`". Adding a conjunct to
# that clause's body would leave the mutation still applicable but change the line a reviewer is told to
# find, and the control is cheap to keep exactly as written.
#
# Second, `approval.require_approval` and `approval.reasons` are asserted to agree in both directions over
# every fixture (`test_require_approval_agrees_with_reasons_*`). An exemption belongs to PRECEDENCE, not to
# whether an approval is called for: a README-only change in production genuinely does trip the production
# clause, and the operator has separately said that particular trip may be waived. Recording it as "no
# approval was required" would erase the reason from the audit trail. So `require_approval` stays true, its
# reason stays populated, and `governance.rego` — the file whose header says it owns precedence between
# sub-policies and owns it "HERE and nowhere else" — decides what to do about it.
#
# WHAT THIS CANNOT DO
# It cannot override a DENY. `governance.result` requires `count(deny_reasons) == 0` in every clause,
# including the two that consult this file, so a blocked window or a protected path still denies whatever
# this says. The exemption only ever converts `require_approval` into `allow`, and only under every
# condition below.
package forgeops.governance.exemption

import rego.v1

# `default applies := false` for the D-25 reason the rest of the bundle states at length: an undefined
# document and a false one are indistinguishable to an HTTP caller, and the fail-closed direction for an
# exemption is "not exempt".
default applies := false

applies if {
	enabled
	documentation_only
	not blocking_blast_radius
}

# The flag must be exactly `true`. `input.project.auto_approve_readme_only` being any other truthy value —
# the string "false", say, which is what a mis-parsed environment variable produces — must not enable it.
default enabled := false

enabled if input.project.auto_approve_readme_only == true

# Every change item must touch documentation, and there must be at least one.
#
# THE COUNT GUARD IS NOT REDUNDANT. `every` over an empty collection is vacuously true, so without it an
# empty change set would satisfy `documentation_only` and take the exemption. An empty change set is
# harmless in itself, but "the exemption applies to a change set with nothing in it" is the shape that
# stops being harmless the moment a caller sends items this bundle cannot read.
default documentation_only := false

documentation_only if {
	count(input.change_items) > 0
	every item in input.change_items {
		documentation_item(item)
	}
}

# One change item that qualifies: a documentation file, not being deleted.
#
# A DELETE NEVER QUALIFIES, even of a README. `approval.rego` requires an approval for any delete as its
# own clause, and that clause is about losing content rather than about which file lost it — a change set
# that removes a project's only documentation is exactly the kind of thing a human should see.
documentation_item(item) if {
	item.action != "delete"
	documentation_path(item.file_path)
}

# A documentation path, decided on the FILE NAME and extension rather than on a substring.
#
# `contains(path, "README")` would have been shorter and wrong in a way that matters: it matches
# `src/readme_parser.py`, `internal/READMEGenerator.go` and `scripts/update-readme.sh`, none of which are
# documentation, and all of which would then be modifiable in production without review. The check is
# therefore anchored on the base name.
documentation_path(path) if {
	is_string(path)
	base := basename(path)

	# `folded` rather than `lower`, because binding a variable named after the builtin it calls is a
	# `rego_compile_error: var lower referenced above`.
	folded := lower(base)
	documentation_name(folded)
}

# The closed list of documentation base names. A closed list rather than "anything under `docs/`", because
# a directory named `docs` can hold a `Dockerfile`, a `docker-compose.yaml`, or a generated `conf.py` that
# runs at build time.
documentation_name(folded) if folded == "readme"

documentation_name(folded) if folded == "readme.md"

documentation_name(folded) if folded == "readme.rst"

documentation_name(folded) if folded == "readme.txt"

documentation_name(folded) if folded == "readme.adoc"

documentation_name(folded) if folded == "changelog.md"

documentation_name(folded) if folded == "contributing.md"

documentation_name(folded) if folded == "code_of_conduct.md"

documentation_name(folded) if folded == "authors.md"

# `basename` without a stdlib call that assumes a separator.
#
# BOTH SEPARATORS ARE HANDLED because the agent ships for windows/amd64 as well as linux, and a change
# item's `file_path` arriving as `docs\README.md` must classify the same way as `docs/README.md`. A
# `/`-only split would let a Windows path through this rule unrecognised — which fails closed, so it is a
# missed exemption rather than a granted one, but it would also make the same repository behave
# differently on two platforms.
basename(path) := last if {
	normalised := replace(path, "\\", "/")
	parts := split(normalised, "/")
	last := parts[count(parts) - 1]
}

# A present blast radius that is not `allow` blocks the exemption.
#
# The asymmetry mirrors `approval.rego`'s, and for the reason finding 71 records: at stage 1 there is
# genuinely no verdict yet, so an ABSENT `blast_radius` must not disable this — stage 4 blocks a BLOCK
# verdict itself, unconditionally, in the chokepoint. A PRESENT one that says anything other than `allow`
# is a real objection from a stage that has run, and no project setting should waive it.
default blocking_blast_radius := false

blocking_blast_radius if {
	input.blast_radius
	input.blast_radius.verdict != "allow"
}

blocking_blast_radius if {
	input.blast_radius
	not input.blast_radius.verdict
}

# The human-readable half, for the same reason `approval.reasons` exists (FR-37). An exemption that
# changes a decision without saying so is indistinguishable from a decision that never needed approval.
default reason := ""

reason := sprintf(
	"documentation-only change set (%d item(s)) auto-approved by the project's auto_approve_readme_only setting",
	[count(input.change_items)],
) if {
	applies
}
