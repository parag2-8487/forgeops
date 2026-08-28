# SPDX-License-Identifier: FSL-1.1-ALv2
"""Starting points a user adapts when writing a policy.

WHAT THESE ARE, AND WHAT THEY ARE NOT
-------------------------------------
A template supplies two things: the parameter names the governance bundle reads, and Rego a user can
read to understand what those parameters do. Enforcement itself lives in the published bundle —
`policies/agent/schedule.rego` and `policies/agent/paths.rego` — which is evaluated by OPA on both
sides. A stored policy's own `rego_rules` is what the dry-run endpoint (`POST /policies/{id}/test`)
evaluates, so the text here has to actually work when asked.

TWO DEFECTS FIXED HERE, both of which made these templates worse than nothing.

1.  The scheduling template contained `_current_weekday = "Saturday" # Simplified for template`. A
    hardcoded weekday is fabricated data on a path a user adopts: whatever day they configured, the
    rule compared it against Saturday. Someone blocking Friday deploys would have got a policy that
    silently governed Saturdays. It now derives the weekday from `input.now_rfc3339`, which is the
    field the chokepoint actually sends, using OPA's own `time` builtins.

2.  Both templates declared `package forgeops.governance`, the same package as the real bundle. A
    published policy in that package would not extend the bundle's `decision` rule, it would COLLIDE
    with it — two `default decision` definitions in one package is a compile error, and two partial
    definitions would silently combine. They now use `forgeops.policy.<name>`, so a template is
    evaluable on its own and cannot corrupt the governance decision if published.

The file-restrictions template also compared `input.change_items[_].path == input.project.protected_globs[_]`,
which is exact string equality against a value called a *glob*. `**/package.json` would therefore have
matched nothing, because no file is literally named that. It now uses `glob.match`, which is what the
real `paths.rego` does.
"""

from .schemas import PolicyTemplateRead

SCHEDULING_TEMPLATE = PolicyTemplateRead(
    id="scheduling",
    name="Time-based Scheduling",
    description=(
        "Blocks operations on the weekdays you name. The weekday is evaluated in the project's "
        "timezone, from the timestamp the chokepoint sends with every mutation."
    ),
    rego_rules="""package forgeops.policy.scheduling

# The weekday comes from the request, not from a constant. An earlier version of this template
# hardcoded "Saturday", so a policy blocking Friday deploys governed the wrong day entirely.
default decision := "allow"

decision := "deny" if {
	blocked_today
}

# `input.project.timezone` is optional; UTC is the honest default because it is the one timezone
# that needs no configuration to be correct about an instant.
timezone := input.project.timezone if {
	input.project.timezone != ""
} else := "UTC"

current_weekday := time.weekday(time.parse_rfc3339_ns(input.now_rfc3339))

blocked_today if {
	some day in input.project.blocked_weekdays
	day == current_weekday
}
""",
    parameters={
        "blocked_weekdays": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "Sunday",
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                    "Saturday",
                ],
            },
            "description": "Weekday names as OPA's time.weekday returns them.",
        },
        "timezone": {
            "type": "string",
            "description": "IANA name, e.g. Asia/Kolkata. Defaults to UTC when absent.",
        },
    },
)

FILE_RESTRICTIONS_TEMPLATE = PolicyTemplateRead(
    id="file_restrictions",
    name="File Restrictions",
    description=(
        "Blocks changes that touch the paths you name. Patterns are globs, so `**/package.json` "
        "matches the file at any depth."
    ),
    rego_rules="""package forgeops.policy.file_restrictions

default decision := "allow"

decision := "deny" if {
	touches_protected_path
}

# `glob.match` rather than `==`. An earlier version compared a file path to the pattern with string
# equality, so `**/package.json` matched nothing at all: no file is literally named that.
touches_protected_path if {
	some item in input.change_items
	some pattern in input.project.protected_globs
	glob.match(pattern, ["/"], item.path)
}
""",
    parameters={
        "protected_globs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Glob patterns, matched against each change item's path with `/` as the separator.",
        },
    },
)

TEMPLATES = [SCHEDULING_TEMPLATE, FILE_RESTRICTIONS_TEMPLATE]
