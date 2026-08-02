# policies/agent/schedule.rego — phases.md §1.7's "Never deploy on Fridays".
#
# Design: §11.7; Deliverable 1.7; criterion 7.
#
# DATA-DRIVEN, NOT LITERAL
# "Friday" appears nowhere in this file. The blocked weekdays, the hour window and the
# set of operations the window governs all arrive on `input.project`, so one rule serves
# every project and a project that blocks Sunday evenings needs no new Rego. That is
# what task 9.1's "data-driven rules" asks for, and it is also what makes Q-06 able to
# generate weekdays and timezones rather than assert one calendar.
#
# THE TIMEZONE IS APPLIED, NOT DECORATIVE (finding 67)
# §11.7's sketch computed `day := time.weekday(time.parse_ns(...))` and then interpolated
# `tz` into the message. `time.weekday` given a bare nanosecond count answers in **UTC**,
# so that sketch would have named the project's timezone in a verdict computed without it
# — and a UTC Friday is a Thursday evening in Los Angeles and a Friday morning in
# Kolkata. Both forms are here: `time.weekday([ns, tz])` and `time.clock([ns, tz])`, so
# the weekday and the hour are both local to the project. `governance_test.rego` pins one
# instant that is a Friday inside the window in one zone, a Friday OUTSIDE it in another,
# and not a Friday at all in a third, which is the assertion the UTC form cannot pass.
#
# `default allow := false` for the D-25 reason `governance.rego` states at length: a deny
# from this sub-policy has to be a defined `false`, not an absent document.
package forgeops.governance.schedule

import rego.v1

default allow := false

allow if deny_reason == ""

default deny_reason := ""

deny_reason := sprintf(
	"blocked deployment window: %s %02d:00-%02d:00 in %s",
	[local_weekday, window.start_hour, window.end_hour, timezone],
) if {
	input.operation in gated_operations
	local_weekday in blocked_weekdays
	local_hour >= window.start_hour
	local_hour < window.end_hour
}

# An unusable timestamp BLOCKS a gated operation rather than permitting it. Without this
# clause every expression below is undefined for a missing or malformed `now_rfc3339`,
# `deny_reason` falls back to its `""` default, and the entry document reads that as
# "the schedule does not object" — a policy that can be switched off by omitting a field.
# OPA's default builtin-error behaviour is to make the call undefined rather than to
# abort, which is precisely why the absence has to be tested for explicitly.
deny_reason := "schedule window cannot be evaluated: input.now_rfc3339 is absent or not RFC 3339" if {
	input.operation in gated_operations
	not evaluable_instant
}

evaluable_instant if is_number(time.parse_rfc3339_ns(input.now_rfc3339))

# ─── Parameters, each total so a project that omits one gets a defined answer ────

# Absent or empty timezone falls back to UTC rather than leaving every downstream
# expression undefined. An undefined `deny_reason` would be indistinguishable from "not
# blocked" to a caller reading the default, so the failure mode of a missing parameter
# must not be silence.
timezone := input.project.timezone if {
	input.project.timezone != ""
} else := "UTC"

blocked_weekdays := {d | some d in input.project.blocked_weekdays}

# The five mutating operations of §7.7, which is exactly the set a deployment window can
# meaningfully block: refusing a `scan.full` on a Friday would stop the platform reading
# the project without stopping it changing anything. A project may narrow or widen it.
default_gated_operations := {
	"changeset.apply",
	"changeset.revert",
	"git.branch_commit_push",
	"git.open_pr",
	"secrets.inject",
}

gated_operations := {op | some op in input.project.blocked_operations} if {
	count(input.project.blocked_operations) > 0
} else := default_gated_operations

# Half-open [start_hour, end_hour) in project-local time. An absent window means the
# whole local day, which is what "never deploy on Fridays" means with no window given.
window := {
	"start_hour": start_hour,
	"end_hour": end_hour,
}

start_hour := input.project.blocked_window.start_hour if {
	is_number(input.project.blocked_window.start_hour)
} else := 0

end_hour := input.project.blocked_window.end_hour if {
	is_number(input.project.blocked_window.end_hour)
} else := 24

# ─── The instant, read in the project's zone ────────────────────────────────────

# `time.parse_rfc3339_ns` rather than §11.7's `time.parse_ns` with an explicit layout:
# the layout form rejects a fractional-second timestamp, which `datetime.now(UTC)` on the
# Python side and `time.Now().Format(time.RFC3339Nano)` on the Go side both produce. A
# governance verdict that depends on whether the caller's clock happened to land on a
# whole second is not a verdict.
now_ns := time.parse_rfc3339_ns(input.now_rfc3339)

local_weekday := time.weekday([now_ns, timezone])

local_hour := time.clock([now_ns, timezone])[0]
