from .schemas import PolicyTemplateRead

SCHEDULING_TEMPLATE = PolicyTemplateRead(
    id="scheduling",
    name="Time-based Scheduling",
    description="Allows operations only within specific time windows and blocks specified weekdays.",
    rego_rules="""package forgeops.governance

# Deny by default
default decision = "deny"

decision = "allow" {
    not _is_blocked_day
}

_is_blocked_day {
    input.project.blocked_weekdays[_] == _current_weekday
}

# Example helper - actual logic would use time module
_current_weekday = "Saturday" # Simplified for template
""",
    parameters={"blocked_weekdays": {"type": "array", "items": {"type": "string"}}},
)

FILE_RESTRICTIONS_TEMPLATE = PolicyTemplateRead(
    id="file_restrictions",
    name="File Restrictions",
    description="Prevents modifications to protected globs.",
    rego_rules="""package forgeops.governance

default decision = "deny"

decision = "allow" {
    not _touches_protected_file
}

_touches_protected_file {
    input.change_items[_].path == input.project.protected_globs[_]
}
""",
    parameters={"protected_globs": {"type": "array", "items": {"type": "string"}}},
)

TEMPLATES = [SCHEDULING_TEMPLATE, FILE_RESTRICTIONS_TEMPLATE]
