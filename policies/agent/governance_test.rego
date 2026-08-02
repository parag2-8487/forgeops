# policies/agent/governance_test.rego — the governance bundle's own tests.
#
# Run by `make policy-test` and by the `policy` CI job:
#   opa test policies/ --ignore '*.yaml' -v
#   opa check --strict --ignore '*.yaml' policies/
#
# Design: §8.3, §11.7, §13.4; task 9.1; criterion 7.
#
# WHAT IS DELIBERATELY ASSERTED HERE RATHER THAN LEFT TO Q-06
# Q-06 (leaf 9.6) proves the two EVALUATORS agree. It cannot prove either of them is
# right: two evaluators running the same wrong rule agree perfectly. So the calendar
# arithmetic, the glob matching, the approval triggers, the precedence order and the
# totality of the entry document are pinned here, in the bundle's own language, and Q-06
# quantifies over agreement on top of that.
package forgeops.governance_test

import rego.v1

import data.forgeops.governance
import data.forgeops.governance.approval
import data.forgeops.governance.paths
import data.forgeops.governance.schedule

# ─── Fixtures ───────────────────────────────────────────────────────────────────

# 2026-08-07T02:30:00Z is a Friday in UTC. The same instant is Friday 08:00 in Kolkata
# (+05:30) and Thursday 19:30 in Los Angeles (-07:00, PDT) — which is what makes one
# timestamp enough to cover "Friday inside the window", "Friday outside the window" and
# "not Friday at all" without three different calendars.
friday_0230_utc := "2026-08-07T02:30:00Z"

# A working-hours window, so "outside the window" is reachable on a blocked weekday. With
# no window a blocked weekday is blocked all day, which is the other case below.
project(tz) := {
	"timezone": tz,
	"blocked_weekdays": ["Friday"],
	"blocked_window": {"start_hour": 6, "end_hour": 20},
}

# Least-privilege baseline: an allowed blast radius, a non-prod environment and no change
# items, so any deny or require_approval a test observes came from the clause it is about.
base(tz) := {
	"operation": "changeset.apply",
	"now_rfc3339": friday_0230_utc,
	"project": project(tz),
	"change_items": [],
	"environment": "dev",
	"blast_radius": {"verdict": "allow"},
}

# ─── schedule: Friday inside and outside the window, across timezones ───────────

test_friday_inside_the_window_is_denied if {
	d := governance.decision with input as base("Asia/Kolkata")
	d.result == "deny"
	d.rule == "schedule.blocked_window"
	d.reason == "blocked deployment window: Friday 06:00-20:00 in Asia/Kolkata"
}

test_friday_outside_the_window_is_allowed if {
	d := governance.decision with input as base("UTC")
	d.result == "allow"
	d.rule == "governance.allow"
	d.reason == ""
}

test_the_same_instant_is_not_friday_in_another_zone if {
	d := governance.decision with input as base("America/Los_Angeles")
	d.result == "allow"
}

# Finding 67, asserted directly rather than only through its consequences: §11.7's sketch
# read the weekday from a bare nanosecond count, which answers in UTC whatever the
# project's timezone says. This test fails against that form, because all three would be
# "Friday" and all three hours would be 2.
test_the_weekday_and_hour_are_local_to_the_project if {
	schedule.local_weekday == "Friday" with input as base("UTC")
	schedule.local_hour == 2 with input as base("UTC")
	schedule.local_weekday == "Friday" with input as base("Asia/Kolkata")
	schedule.local_hour == 8 with input as base("Asia/Kolkata")
	schedule.local_weekday == "Thursday" with input as base("America/Los_Angeles")
	schedule.local_hour == 19 with input as base("America/Los_Angeles")
}

test_a_blocked_weekday_with_no_window_is_blocked_all_day if {
	inp := json.patch(base("UTC"), [{
		"op": "remove",
		"path": "/project/blocked_window",
	}])

	d := governance.decision with input as inp
	d.result == "deny"
	d.reason == "blocked deployment window: Friday 00:00-24:00 in UTC"
}

# The control for every schedule test above: a policy that blocked everything would pass
# them all. Wednesday is not in `blocked_weekdays`.
test_an_unblocked_weekday_is_allowed if {
	inp := json.patch(base("UTC"), [{
		"op": "replace",
		"path": "/now_rfc3339",
		"value": "2026-08-05T10:00:00Z",
	}])

	d := governance.decision with input as inp
	d.result == "allow"
}

# A read-only operation must not be stopped by a deployment window: refusing `scan.full`
# on a Friday would stop the platform reading the project without stopping it changing
# anything. `scan.full` is outside §7.7's mutating set and therefore outside the default
# gated set.
test_a_non_mutating_operation_is_not_gated_by_the_window if {
	inp := json.patch(base("Asia/Kolkata"), [{
		"op": "replace",
		"path": "/operation",
		"value": "scan.full",
	}])

	d := governance.decision with input as inp
	d.result == "allow"
}

test_the_gated_operation_set_is_data_driven if {
	inp := json.patch(base("Asia/Kolkata"), [
		{"op": "replace", "path": "/operation", "value": "scan.full"},
		{"op": "add", "path": "/project/blocked_operations", "value": ["scan.full"]},
	])

	d := governance.decision with input as inp
	d.result == "deny"
	d.rule == "schedule.blocked_window"
}

# Fail-closed on an unusable instant (the clause `schedule.rego` explains): without it,
# omitting `now_rfc3339` switches the schedule policy off.
test_a_missing_timestamp_blocks_a_gated_operation if {
	inp := json.patch(base("UTC"), [{
		"op": "remove",
		"path": "/now_rfc3339",
	}])

	d := governance.decision with input as inp
	d.result == "deny"
	d.rule == "schedule.blocked_window"
	d.reason == "schedule window cannot be evaluated: input.now_rfc3339 is absent or not RFC 3339"
}

test_a_malformed_timestamp_blocks_a_gated_operation if {
	inp := json.patch(base("UTC"), [{
		"op": "replace",
		"path": "/now_rfc3339",
		"value": "next Friday",
	}])

	d := governance.decision with input as inp
	d.result == "deny"
	d.rule == "schedule.blocked_window"
}

# A fractional-second timestamp is what both runtimes actually produce
# (`datetime.now(UTC).isoformat()`, `time.Now().Format(time.RFC3339Nano)`). §11.7's
# `time.parse_ns` layout form rejects it, which would have made the verdict depend on
# whether the caller's clock landed on a whole second.
test_a_fractional_second_timestamp_is_understood if {
	inp := json.patch(base("Asia/Kolkata"), [{
		"op": "replace",
		"path": "/now_rfc3339",
		"value": "2026-08-07T02:30:00.123456789Z",
	}])

	d := governance.decision with input as inp
	d.result == "deny"
	d.rule == "schedule.blocked_window"
}

# ─── paths: package.json protection ─────────────────────────────────────────────

protected(items) := json.patch(base("UTC"), [
	{"op": "replace", "path": "/now_rfc3339", "value": "2026-08-05T10:00:00Z"},
	{"op": "add", "path": "/project/protected_globs", "value": ["**/package.json", "**/go.mod"]},
	{"op": "replace", "path": "/change_items", "value": items},
])

test_package_json_is_protected if {
	d := governance.decision with input as protected([{"file_path": "package.json", "action": "modify"}])
	d.result == "deny"
	d.rule == "paths.protected_path"
	d.reason == "protected path: package.json"
}

test_a_nested_package_json_is_protected if {
	d := governance.decision with input as protected([{"file_path": "frontend/package.json", "action": "modify"}])
	d.result == "deny"
	d.reason == "protected path: frontend/package.json"
}

# The control: a project whose globs match nothing must not block an ordinary edit, and
# `package-lock.json` is the near miss a `package.json*` glob would wrongly catch.
test_an_unprotected_path_is_allowed if {
	d := governance.decision with input as protected([
		{"file_path": "src/index.ts", "action": "modify"},
		{"file_path": "package-lock.json", "action": "modify"},
	])
	d.result == "allow"
}

# `["/"]` as the delimiter is what makes `*` stop at a path segment. Without it a single
# `*` would span separators and `**/package.json` would be indistinguishable from
# `*package.json`.
test_a_single_star_does_not_span_path_separators if {
	inp := json.patch(protected([{"file_path": "a/b/package.json", "action": "modify"}]), [{
		"op": "replace",
		"path": "/project/protected_globs",
		"value": ["*/package.json"],
	}])

	d := governance.decision with input as inp
	d.result == "allow"
}

# The message is sorted and joined rather than printed from a set, so two evaluators and
# two readers see one ordering.
test_several_violations_are_reported_in_sorted_order if {
	d := governance.decision with input as protected([
		{"file_path": "z/package.json", "action": "modify"},
		{"file_path": "a/package.json", "action": "modify"},
		{"file_path": "go.mod", "action": "modify"},
	])
	d.reason == "protected path: a/package.json, go.mod, z/package.json"
}

test_a_project_with_no_protected_globs_blocks_nothing if {
	inp := json.patch(base("UTC"), [
		{"op": "replace", "path": "/now_rfc3339", "value": "2026-08-05T10:00:00Z"},
		{"op": "replace", "path": "/change_items", "value": [{"file_path": "package.json", "action": "modify"}]},
	])

	d := governance.decision with input as inp
	d.result == "allow"
}

# Finding 69, both directions. `**/package.json` catches the root file and the nested one;
# a pattern written without the prefix still means exactly what it says.
test_a_double_star_prefix_covers_the_root_and_below if {
	paths.matches("**/package.json", "package.json")
	paths.matches("**/package.json", "frontend/package.json")
	paths.matches("**/package.json", "a/b/c/package.json")
}

test_a_pattern_without_the_prefix_is_unchanged if {
	paths.matches("package.json", "package.json")
	not paths.matches("package.json", "frontend/package.json")
	not paths.matches("*/package.json", "package.json")
	not paths.matches("*/package.json", "a/b/package.json")
}

# ─── approval: production, blast radius, deletes ────────────────────────────────

wednesday(patches) := json.patch(base("UTC"), array.concat(
	[{"op": "replace", "path": "/now_rfc3339", "value": "2026-08-05T10:00:00Z"}],
	patches,
))

test_prod_requires_approval if {
	d := governance.decision with input as wednesday([{"op": "replace", "path": "/environment", "value": "prod"}])
	d.result == "require_approval"
	d.rule == "approval.required"
	d.reason == "environment is \"prod\""
}

test_a_non_allow_blast_radius_requires_approval if {
	d := governance.decision with input as wednesday([{
		"op": "replace",
		"path": "/blast_radius/verdict",
		"value": "block",
	}])
	d.result == "require_approval"
	d.reason == "blast-radius verdict is \"block\", not \"allow\""
}

test_a_delete_requires_approval if {
	d := governance.decision with input as wednesday([{
		"op": "replace",
		"path": "/change_items",
		"value": [{"file_path": "src/old.ts", "action": "delete"}],
	}])
	d.result == "require_approval"
	d.reason == "change item deletes src/old.ts"
}

# Finding 68: an absent field must not read as permission. Both of these pass trivially
# against §11.7's sketch, which is why they are here.
test_a_blast_radius_object_with_no_verdict_requires_approval if {
	d := governance.decision with input as wednesday([{
		"op": "replace",
		"path": "/blast_radius",
		"value": {"score": 12},
	}])
	d.result == "require_approval"
	d.reason == "blast-radius verdict is absent"
}

# Finding 71, the one exception, and the reason it is not a hole: §2.2 runs this bundle at
# stage 1 and computes the blast radius at stage 4, so an entirely absent member is the
# normal stage-1 shape. Stage 4 blocks a BLOCK verdict itself, in the chokepoint.
test_an_entirely_absent_blast_radius_is_left_to_stage_four if {
	d := governance.decision with input as wednesday([{"op": "remove", "path": "/blast_radius"}])
	d.result == "allow"
}

test_an_absent_environment_requires_approval if {
	d := governance.decision with input as wednesday([{"op": "remove", "path": "/environment"}])
	d.result == "require_approval"
	d.reason == "environment is absent"
}

test_several_approval_triggers_are_all_reported if {
	d := governance.decision with input as wednesday([
		{"op": "replace", "path": "/environment", "value": "prod"},
		{"op": "replace", "path": "/blast_radius/verdict", "value": "review"},
	])
	d.result == "require_approval"
	d.reason == "blast-radius verdict is \"review\", not \"allow\"; environment is \"prod\""
}

# ─── Precedence, owned by governance.rego and by nothing else ───────────────────

# A malformed input outranks everything, so a caller cannot learn about a schedule window
# by sending a request the policy could not evaluate.
test_a_malformed_input_outranks_a_schedule_block if {
	inp := json.patch(base("Asia/Kolkata"), [{"op": "remove", "path": "/operation"}])

	d := governance.decision with input as inp
	d.result == "deny"
	d.rule == "governance.malformed_input"
}

test_a_block_outranks_an_approval if {
	inp := json.patch(base("Asia/Kolkata"), [{
		"op": "replace",
		"path": "/environment",
		"value": "prod",
	}])

	d := governance.decision with input as inp
	d.result == "deny"
	d.rule == "schedule.blocked_window"
}

test_schedule_outranks_paths if {
	inp := json.patch(base("Asia/Kolkata"), [
		{"op": "add", "path": "/project/protected_globs", "value": ["**/package.json"]},
		{"op": "replace", "path": "/change_items", "value": [{"file_path": "package.json", "action": "modify"}]},
	])

	d := governance.decision with input as inp
	d.rule == "schedule.blocked_window"
	startswith(d.reason, "blocked deployment window: Friday 06:00-20:00 in Asia/Kolkata")
}

test_both_blocking_reasons_are_reported_when_both_apply if {
	inp := json.patch(base("Asia/Kolkata"), [
		{"op": "add", "path": "/project/protected_globs", "value": ["**/package.json"]},
		{"op": "replace", "path": "/change_items", "value": [{"file_path": "package.json", "action": "modify"}]},
	])

	governance.deny_reasons == [
		"blocked deployment window: Friday 06:00-20:00 in Asia/Kolkata",
		"protected path: package.json",
	] with input as inp
}

# ─── D-25: a deny is a DEFINED false, in all four documents ─────────────────────
#
# `not allow` is satisfied by an UNDEFINED document just as well as by a false one, so a
# test written that way would pass against the exact bug D-25 exists to prevent. These
# compare against `false`, which an undefined document cannot satisfy.

test_governance_allow_is_a_defined_false_when_denied if {
	governance.allow == false with input as base("Asia/Kolkata")
}

test_schedule_allow_is_a_defined_false_when_it_blocks if {
	schedule.allow == false with input as base("Asia/Kolkata")
}

test_paths_allow_is_a_defined_false_when_it_blocks if {
	paths.allow == false with input as protected([{"file_path": "package.json", "action": "modify"}])
}

test_approval_allow_is_a_defined_false_when_approval_is_required if {
	approval.allow == false with input as wednesday([{"op": "replace", "path": "/environment", "value": "prod"}])
}

test_all_four_allow_documents_are_defined_true_on_a_clean_input if {
	governance.allow == true with input as base("UTC")
	schedule.allow == true with input as base("UTC")
	paths.allow == true with input as base("UTC")
	approval.allow == true with input as base("UTC")
}

# ─── Totality, including for garbage ────────────────────────────────────────────
#
# "Total" has to mean total for an input the caller got wrong, because that is the input
# a misconfigured deployment sends. An empty object exercises every default at once.
#
# Note what these assert BEYOND definedness: the answer for a malformed input is `deny`
# and not `require_approval`. Before `input_error` existed, `{}` came back
# `require_approval` — because the blast-radius verdict is absent and finding 68's clause
# fires — which is defined, and safe in the sense that it is not `allow`, and still wrong:
# it would have the chokepoint open an approval flow for an operation nobody named.

test_the_entry_document_is_total_for_an_empty_input if {
	d := governance.decision with input as {}
	d.result == "deny"
	d.rule == "governance.malformed_input"
	d.reason == "governance input is malformed: input.operation must be a string"
	governance.allow == false with input as {}
}

test_the_entry_document_is_total_for_a_wrongly_typed_input if {
	garbage := {
		"operation": 7,
		"now_rfc3339": ["not", "a", "timestamp"],
		"project": "not an object",
		"change_items": "not a list",
		"environment": {"deeply": "wrong"},
		"blast_radius": 3,
	}

	d := governance.decision with input as garbage
	d.result == "deny"
	d.rule == "governance.malformed_input"
	governance.allow == false with input as garbage
}

test_a_named_operation_with_nothing_else_is_not_malformed if {
	d := governance.decision with input as {"operation": "scan.full"}
	d.rule == "approval.required"
	d.result == "require_approval"
}

# The control for the two totality tests above: they must be failing on `input_error` and
# not on some other clause that happens to deny. A gated operation with no timestamp is
# well-formed and still denied — by the schedule clause, named as such.
test_a_named_gated_operation_with_no_timestamp_is_denied_by_the_schedule_not_by_malformedness if {
	d := governance.decision with input as {"operation": "changeset.apply"}
	d.result == "deny"
	d.rule == "schedule.blocked_window"
}

# ─── The two statements of the approval fact must agree (journal pattern H) ─────
#
# `require_approval`'s clauses are written out because Q-06's negative control is worded
# against one of those lines. That leaves `reasons` as a second statement of the same
# fact, and pattern H's rule is that the relationship gets asserted in both directions.

every_fixture := [
	{},
	base("UTC"),
	base("Asia/Kolkata"),
	base("America/Los_Angeles"),
	wednesday([{"op": "replace", "path": "/environment", "value": "prod"}]),
	wednesday([{"op": "replace", "path": "/blast_radius/verdict", "value": "block"}]),
	wednesday([{"op": "replace", "path": "/blast_radius", "value": {"score": 12}}]),
	wednesday([{"op": "remove", "path": "/blast_radius"}]),
	wednesday([{"op": "remove", "path": "/environment"}]),
	wednesday([{
		"op": "replace",
		"path": "/change_items",
		"value": [{"file_path": "src/old.ts", "action": "delete"}],
	}]),
	protected([{"file_path": "package.json", "action": "modify"}]),
]

test_require_approval_implies_at_least_one_reason if {
	every fixture in every_fixture {
		approval_and_reasons_agree(fixture)
	}
}

approval_and_reasons_agree(fixture) if {
	approval.require_approval with input as fixture
	count(approval.reasons) > 0 with input as fixture
}

approval_and_reasons_agree(fixture) if {
	not approval.require_approval with input as fixture
	count(approval.reasons) == 0 with input as fixture
}

# The control for the clause above: a fixture list on which `require_approval` were never
# true would satisfy it vacuously. Both halves must be witnessed.
test_the_fixture_list_witnesses_both_halves if {
	some required in every_fixture
	approval.require_approval with input as required

	some permitted in every_fixture
	not approval.require_approval with input as permitted
}
