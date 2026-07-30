#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/check-gitleaks-config.sh — keep .gitleaks.toml narrow.
#
# Why
# ---
# An allowlist is the cheapest way to make a secret scan green, and therefore the easiest
# thing to widen under pressure. Four widenings turn the scan into decoration:
#
#   useDefault = false          discards every built-in rule
#   stopwords                   suppress a token in every file, everywhere
#   an entry with no `paths`     applies to the whole repository
#   an entry with no `regexes`   applies to every value the rule finds
#
# The committed config excepts exactly two placeholder values in two files. This check
# fails the build if any entry loses its scoping or if the defaults are switched off, so
# the narrowness is enforced rather than intended. It is the same reasoning
# scripts/check-test-doubles.py applies to a reasonless `# noqa`.
#
# Failure is exit 1 naming the offending entry. Exit 1 also when the file declares NO
# allowlist at all, because a parser that stopped recognising the syntax would otherwise
# report success forever — the vacuity trap design.md 0.4.5 closes for the mutation harness.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="${1:-$REPO_ROOT/.gitleaks.toml}"

if [ ! -f "$CONFIG" ]; then
	printf 'ERROR: %s not found\n' "$CONFIG" >&2
	exit 1
fi

printf '==> check-gitleaks-config: %s\n' "$CONFIG"

FAILED=0

# --- useDefault must be true -------------------------------------------------
if ! grep -qE '^[[:space:]]*useDefault[[:space:]]*=[[:space:]]*true' "$CONFIG"; then
	printf 'ERROR: useDefault is not true; every built-in rule would be discarded\n' >&2
	FAILED=1
fi
if grep -qE '^[[:space:]]*useDefault[[:space:]]*=[[:space:]]*false' "$CONFIG"; then
	printf 'ERROR: useDefault = false\n' >&2
	FAILED=1
fi

# --- stopwords are never acceptable ------------------------------------------
# Matched as a KEY assignment, so the word may still be discussed in a comment or a
# description — which this file does, to explain why it is banned.
if grep -qE '^[[:space:]]*stopwords[[:space:]]*=' "$CONFIG"; then
	printf 'ERROR: stopwords suppress a token in every file; use a scoped allowlist instead\n' >&2
	FAILED=1
fi

# --- every allowlist entry needs both `paths` and `regexes` ------------------
# Walked with awk rather than a TOML parser so the check has no dependency beyond the
# coreutils every other check-*.sh already relies on. Section boundaries are the `[[`/`[`
# lines, which is enough for a file this shape.
awk '
	function flush() {
		if (in_allowlist) {
			label = (desc != "" ? desc : ("entry at line " start_line))
			if (!has_paths)   printf("ERROR: allowlist (%s) has no `paths`; it applies to the whole repository\n", label) > "/dev/stderr"
			if (!has_regexes) printf("ERROR: allowlist (%s) has no `regexes`; it applies to every value the rule finds\n", label) > "/dev/stderr"
			if (!has_paths || !has_regexes) bad++
			total++
		}
		in_allowlist = 0; has_paths = 0; has_regexes = 0; desc = ""
	}

	/^[[:space:]]*\[\[allowlists?\]\]/ { flush(); in_allowlist = 1; start_line = NR; next }
	/^[[:space:]]*\[/                  { flush(); next }

	in_allowlist && /^[[:space:]]*paths[[:space:]]*=/   { has_paths = 1 }
	in_allowlist && /^[[:space:]]*regexes[[:space:]]*=/ { has_regexes = 1 }
	in_allowlist && /^[[:space:]]*description[[:space:]]*=/ {
		line = $0
		sub(/^[[:space:]]*description[[:space:]]*=[[:space:]]*/, "", line)
		gsub(/["'"'"']/, "", line)
		if (length(line) > 0) desc = substr(line, 1, 60)
	}

	END {
		flush()
		if (total == 0) {
			print "ERROR: no allowlist entry was recognised; the parser or the file changed shape" > "/dev/stderr"
			exit 1
		}
		printf("ok:   %d allowlist entr%s, %d unscoped\n", total, (total == 1 ? "y" : "ies"), bad)
		if (bad > 0) exit 1
	}
' "$CONFIG" || FAILED=1

# --- a rule must not be disabled outright ------------------------------------
if grep -qE '^[[:space:]]*disabledRules[[:space:]]*=' "$CONFIG"; then
	printf 'ERROR: disabledRules removes detection entirely; except the value, not the rule\n' >&2
	FAILED=1
fi

if [ "$FAILED" -ne 0 ]; then
	printf '\ncheck-gitleaks-config: FAILED\n' >&2
	exit 1
fi

printf 'check-gitleaks-config: the allowlist is scoped and the defaults are on\n'
