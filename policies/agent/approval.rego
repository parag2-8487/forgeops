# policies/agent/approval.rego — phases.md §1.7's "Require approval for production".
#
# Design: §11.7; Deliverable 1.7; criterion 7.
#
# WHY THE THREE CLAUSES ARE WRITTEN OUT RATHER THAN DERIVED FROM `reasons`
# Q-06's negative control is worded against a specific line — "invert the comparison in
# `approval.rego`'s `require_approval if input.environment == "prod"` clause to `!=`"
# (Appendix B). So that clause exists here verbatim and a reviewer can find it. The cost
# is that `require_approval` and `reasons` are two statements of one fact, which is
# journal pattern H, and it is paid for the way pattern H says to pay for it: the
# relationship is asserted in BOTH directions by
# `governance_test.rego`'s `test_require_approval_agrees_with_reasons_*`, over every
# fixture, so a clause added to one and forgotten in the other fails the policy job.
#
# FAIL-CLOSED ON AN ABSENT FIELD (finding 68), AND ITS ONE EXCEPTION (finding 71)
# §11.7's sketch had `require_approval if input.blast_radius.verdict != "allow"` and nothing
# else. For an input whose `blast_radius` object is present but carries no `verdict` that
# expression is UNDEFINED, the rule does not fire, and `default require_approval := false`
# answers "no approval needed" — so dropping a field out of the input document was a way to
# skip the approval gate. The `not input.environment` clause below closes the same hole for
# the environment.
#
# The one exception is an ENTIRELY absent `blast_radius`, which is not a mistake: §2.2 runs
# this bundle at stage 1 and computes the blast radius at stage 4, so at evaluation time
# there is genuinely no verdict. See the comment on the clause itself.
#
# The fail-closed clauses are kept syntactically separate from the `!=` and `==` clauses on
# purpose, so Q-06's control still bites on exactly the line Appendix B names.
#
# `default allow := false` for the D-25 reason `governance.rego` states at length. Here
# `allow` means "permitted with no human in the loop", which is why it is the negation of
# `require_approval` rather than a third opinion.
package forgeops.governance.approval

import rego.v1

default allow := false

allow if not require_approval

default require_approval := false

require_approval if input.blast_radius.verdict != "allow"

# `input.blast_radius` is OPTIONAL by contract, and the asymmetry below is deliberate
# (finding 71, landed with leaf 9.2). §2.2's stage order evaluates policy at **stage 1** and
# runs the Semantic Plan Analyzer at **stage 4**, so at the moment this bundle is asked no
# verdict exists; A.11's input document shows one because it describes the agreement check,
# not the transit. Requiring an approval whenever the member is absent — which is what this
# file did when leaf 9.1 shipped it — makes A.3's `change_set_auto_approved` path
# unreachable for every caller.
#
# So an ABSENT `blast_radius` means "stage 4 has not run, and it owns this": stage 4 blocks a
# BLOCK verdict itself, unconditionally, in the chokepoint, so nothing is lost. A PRESENT
# `blast_radius` with no readable verdict stays fail-closed, because that is the shape a
# refactor produces and the one that would otherwise silently disable the clause above.
require_approval if {
	input.blast_radius
	not input.blast_radius.verdict
}

require_approval if input.environment == "prod"

require_approval if not input.environment

require_approval if {
	some item in input.change_items
	item.action == "delete"
}

# ─── The human-readable half of FR-37 ───────────────────────────────────────────

reasons contains sprintf("blast-radius verdict is %q, not \"allow\"", [input.blast_radius.verdict]) if {
	input.blast_radius.verdict != "allow"
}

reasons contains "blast-radius verdict is absent" if {
	input.blast_radius
	not input.blast_radius.verdict
}

reasons contains "environment is \"prod\"" if input.environment == "prod"

reasons contains "environment is absent" if not input.environment

reasons contains sprintf("change item deletes %s", [item.file_path]) if {
	some item in input.change_items
	item.action == "delete"
}
