# policies/agent/exemption_test.rego — FR-34's bounds.
#
# The tests are written as the ways the exemption must NOT fire, because that is where its value is. An
# exemption that fires when it should is a convenience; one that fires when it should not is a production
# change that went through without review, and the operator has no way to see why.
package forgeops.governance_test

import rego.v1

import data.forgeops.governance
import data.forgeops.governance.exemption

# A production change set that touches only a README, in a project that has opted in.
readme_in_prod := {
	"operation": "changeset.apply",
	"now_rfc3339": "2026-09-01T10:00:00Z",
	"environment": "prod",
	"project": {"auto_approve_readme_only": true},
	"change_items": [{"file_path": "README.md", "action": "modify"}],
}

test_the_exemption_applies_to_a_documentation_only_change if {
	exemption.applies with input as readme_in_prod
}

test_the_exemption_turns_require_approval_into_allow if {
	# Without the flag this is `require_approval`, by the production clause.
	governance.result == "require_approval" with input as json.remove(readme_in_prod, ["project"])

	# With it, allowed — and the rule says which clause decided.
	governance.result == "allow" with input as readme_in_prod
	governance.rule == "exemption.readme_only" with input as readme_in_prod
}

test_the_reason_states_both_facts if {
	# The production clause is still reported. An allowed decision that hid why an approval would have
	# been needed is the audit-trail gap this avoids.
	contains(governance.reason, "environment is \"prod\"") with input as readme_in_prod
	contains(governance.reason, "auto_approve_readme_only") with input as readme_in_prod
}

test_approval_still_says_an_approval_was_required if {
	# `approval.require_approval` is deliberately UNCHANGED. The exemption is a precedence decision, not
	# a claim that no approval was called for, and `test_require_approval_agrees_with_reasons_*` must
	# keep holding.
	data.forgeops.governance.approval.require_approval with input as readme_in_prod
}

# ── the ways it must not fire ────────────────────────────────────────────────────────────────────

test_it_does_not_fire_without_the_flag if {
	not exemption.applies with input as json.remove(readme_in_prod, ["project"])
}

test_it_does_not_fire_when_the_flag_is_false if {
	not exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "replace",
		"path": "/project/auto_approve_readme_only",
		"value": false,
	}])
}

test_a_truthy_string_does_not_enable_it if {
	# What a mis-parsed environment variable produces. `== true` rather than a truthiness test.
	not exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "replace",
		"path": "/project/auto_approve_readme_only",
		"value": "true",
	}])
}

test_a_second_non_documentation_item_disqualifies_the_whole_set if {
	# `every`, not `some`. A change set that edits a README and a Dockerfile is not documentation-only,
	# and the Dockerfile is the reason a human should see it.
	not exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "add",
		"path": "/change_items/-",
		"value": {"file_path": "Dockerfile", "action": "modify"},
	}])
}

test_deleting_a_readme_is_never_exempt if {
	# `approval.rego` requires approval for any delete, and that clause is about losing content rather
	# than which file lost it.
	not exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "replace",
		"path": "/change_items/0/action",
		"value": "delete",
	}])
}

test_an_empty_change_set_is_not_exempt if {
	# `every` over an empty collection is VACUOUSLY TRUE, so without the count guard this would pass.
	not exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "replace",
		"path": "/change_items",
		"value": [],
	}])
}

test_a_path_that_merely_contains_readme_is_not_documentation if {
	# `contains(path, "README")` would have matched all three of these, and each would then have been
	# modifiable in production without review.
	every path in ["src/readme_parser.py", "internal/READMEGenerator.go", "scripts/update-readme.sh"] {
		not exemption.applies with input as json.patch(readme_in_prod, [{
			"op": "replace",
			"path": "/change_items/0/file_path",
			"value": path,
		}])
	}
}

test_a_blocking_blast_radius_defeats_the_exemption if {
	not exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "add",
		"path": "/blast_radius",
		"value": {"verdict": "block"},
	}])
}

test_a_present_blast_radius_with_no_verdict_defeats_the_exemption if {
	# Fail-closed on the shape a refactor produces, matching `approval.rego`'s asymmetry.
	not exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "add",
		"path": "/blast_radius",
		"value": {"score": 3},
	}])
}

test_an_absent_blast_radius_does_not_defeat_it if {
	# Finding 71: at stage 1 there is genuinely no verdict, and stage 4 blocks a BLOCK itself.
	exemption.applies with input as readme_in_prod
}

test_the_exemption_cannot_override_a_protected_path if {
	# `count(deny_reasons) == 0` guards every clause that consults the exemption, so a paths denial
	# still denies. This is the property that keeps the exemption from being a bypass.
	denied := json.patch(readme_in_prod, [{
		"op": "add",
		"path": "/project/protected_globs",
		"value": ["README.md"],
	}])
	governance.result == "deny" with input as denied
	governance.rule == "paths.protected_path" with input as denied
}

test_the_exemption_cannot_override_a_blocked_window if {
	blocked := json.patch(readme_in_prod, [
		{"op": "add", "path": "/project/blocked_weekdays", "value": ["Tuesday"]},
		{"op": "add", "path": "/project/timezone", "value": "UTC"},
	])
	governance.result == "deny" with input as blocked
}

test_the_exemption_cannot_rescue_a_malformed_input if {
	# No operation means the request is malformed, not permissive.
	not governance.allow with input as json.remove(readme_in_prod, ["operation"])
}

test_windows_and_posix_separators_classify_identically if {
	# The agent ships for windows/amd64 too, and the same repository must not behave differently on two
	# platforms.
	exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "replace",
		"path": "/change_items/0/file_path",
		"value": "docs\\README.md",
	}])
	exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "replace",
		"path": "/change_items/0/file_path",
		"value": "docs/README.md",
	}])
}

test_the_other_documentation_names_are_recognised if {
	every name in ["CHANGELOG.md", "CONTRIBUTING.md", "readme", "README.rst", "CODE_OF_CONDUCT.md"] {
		exemption.applies with input as json.patch(readme_in_prod, [{
			"op": "replace",
			"path": "/change_items/0/file_path",
			"value": name,
		}])
	}
}

test_a_dockerfile_under_docs_is_not_documentation if {
	# A closed list of base names rather than "anything under `docs/`", because a directory named `docs`
	# can hold a Dockerfile or a `conf.py` that runs at build time.
	not exemption.applies with input as json.patch(readme_in_prod, [{
		"op": "replace",
		"path": "/change_items/0/file_path",
		"value": "docs/Dockerfile",
	}])
}

test_the_exemption_is_total_over_an_empty_input if {
	# Total for garbage too, like every other entry document in this bundle.
	not exemption.applies with input as {}
	exemption.reason == "" with input as {}
}
