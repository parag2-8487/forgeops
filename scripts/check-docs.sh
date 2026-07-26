#!/bin/sh
# Phase 0 documentation check.
#
# Enforces design.md §2.4 (licence split and the forbidden non-SPDX alias),
# §4.2 (RFC 9457 contract), §4.4 (health vs readiness distinction),
# §14.2 (local-development-only warning in the FIRST paragraph of
# docs/deployment.md), §15.2 (no general Phase 0 user authentication) and
# §17.2 OQ-18 (docs/development.md is the build-rules home).
#
# Read-only: it never creates, moves, formats or deletes anything, and it never
# touches the four authoritative root documents. It prints every violation and
# exits non-zero if there is at least one.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

FAILFILE=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-docs-$$")
: >"$FAILFILE"
trap 'rm -rf "$FAILFILE" "$FLATDIR"' EXIT HUP INT TERM

fail() {
	printf 'FAIL: %s\n' "$1" >&2
	printf 'x\n' >>"$FAILFILE"
}

REQUIRED_DOCS='docs/architecture.md
docs/api.md
docs/development.md
docs/deployment.md'

# flatten <file> — collapse newlines/tabs/runs of spaces so a required phrase still
# matches when Markdown wrapping splits it across lines, and lower-case it so the
# comparison is case-insensitive.
#
# Two portability rules are deliberate here:
#   * grep reads the cached FILE, never a pipe — `grep -q` closes its input early
#     and that kills an upstream `tr` with SIGPIPE, producing false failures;
#   * `-F` is never combined with `-i` — MSYS grep 3.0 (Git Bash) aborts with
#     SIGABRT on `-Fi`. Case-insensitivity comes from lower-casing both sides.
FLATDIR=$(mktemp -d 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-docs-flat-$$")
mkdir -p "$FLATDIR" 2>/dev/null || true

flatten_file() {
	# flatten_file <file> -> prints the path of its cached flattened, lower-cased copy
	_key=$(printf '%s' "$1" | tr -c 'A-Za-z0-9' '_')
	_out="$FLATDIR/$_key"
	if [ ! -f "$_out" ]; then
		tr '\n\t' '  ' <"$1" | tr -s ' ' | tr 'A-Z' 'a-z' >"$_out"
	fi
	printf '%s' "$_out"
}

# has_text <file> <literal text> -> 0 when present (case-insensitive)
has_text() {
	_needle=$(printf '%s' "$2" | tr 'A-Z' 'a-z')
	grep -F -- "$_needle" "$(flatten_file "$1")" >/dev/null 2>&1
}

# require_text <file> <literal text> <why> — case-insensitive, prose wording may vary
require_text() {
	file=$1
	needle=$2
	why=$3
	[ -f "$file" ] || return 0
	if ! has_text "$file" "$needle"; then
		fail "$file must document \"$needle\" ($why)"
	fi
}

# forbid_text <file> <literal text> <why>
forbid_text() {
	file=$1
	needle=$2
	why=$3
	[ -f "$file" ] || return 0
	if has_text "$file" "$needle"; then
		fail "$file must not contain \"$needle\" ($why)"
	fi
}

echo 'Checking the Phase 0 documentation set exists (design §2.3, deliverable 0.1)...'
printf '%s\n' "$REQUIRED_DOCS" | {
	while IFS= read -r doc; do
		[ -n "$doc" ] || continue
		if [ ! -f "$doc" ]; then
			fail "required documentation file is missing: $doc"
		elif [ ! -s "$doc" ]; then
			fail "required documentation file is empty: $doc"
		fi
	done
}

echo 'Checking the local-development-only warning is in the first paragraph of docs/deployment.md (design §14.2)...'
if [ -f docs/deployment.md ]; then
	# First prose paragraph: skip leading headings/blank lines, stop at the next
	# blank line, then flatten wrapping so a phrase split across lines still matches.
	FIRST_PARA=$(awk '
		/^[[:space:]]*$/ { if (started) exit; next }
		/^#/             { if (started) exit; next }
		                 { started = 1; print }
	' docs/deployment.md | tr '\n\t' '  ' | tr -s ' ')
	for phrase in 'local development on a trusted machine only' 'must never be exposed to a network'; do
		case $FIRST_PARA in
		*"$phrase"*) ;;
		*) fail "the first paragraph of docs/deployment.md must contain \"$phrase\" (design §14.2)" ;;
		esac
	done
fi

echo 'Checking the Phase 0 authentication boundary is stated (design §15.2, §14.2)...'
for doc in docs/architecture.md docs/api.md docs/development.md docs/deployment.md; do
	require_text "$doc" 'general user authentication' \
		'Phase 0 verifies tokens only at the MCP gateway and /api/v1/ai/complete; general user authentication is Phase 1'
done
require_text docs/api.md '/api/v1/ai/complete' 'the only non-MCP route that verifies a bearer token'

echo 'Checking the health versus readiness distinction (design §4.4)...'
for doc in docs/architecture.md docs/api.md docs/deployment.md; do
	require_text "$doc" '/health/ready' 'readiness route name'
	require_text "$doc" 'Liveness' 'liveness semantics must be named'
done
require_text docs/architecture.md 'Readiness' 'readiness semantics must be named'
require_text docs/api.md 'Readiness' 'readiness semantics must be named'
require_text docs/deployment.md 'still `200`' 'liveness must stay 200 during a dependency outage'

echo 'Checking the RFC 9457 error contract (design §4.2)...'
require_text docs/api.md 'RFC 9457' 'error contract'
require_text docs/api.md 'application/problem+json' 'problem media type'
require_text docs/api.md 'equals the HTTP status' 'body status must equal the HTTP status'
require_text docs/architecture.md 'RFC 9457' 'error contract is a cross-cutting decision'

echo 'Checking Phase 0 route names are documented (design §4.4, §11.1, §11.4-§11.9)...'
ROUTES='/health
/health/ready
/api/v1/health
/api/v1/openapi.json
/api/v1/mcp
/api/v1/mcp/servers
/api/v1/mcp/apps/{name}
/api/v1/ai/tiers
/api/v1/ai/complete
/api/v1/analysis/plan'
printf '%s\n' "$ROUTES" | {
	while IFS= read -r route; do
		[ -n "$route" ] || continue
		require_text docs/api.md "$route" 'Phase 0 route inventory'
	done
}

echo 'Checking the build prerequisites and build-rules home (design §13.4, §17.2 OQ-18)...'
require_text docs/development.md 'GNU make' 'Makefile prerequisite'
require_text docs/development.md 'POSIX shell' 'Makefile prerequisite'
require_text docs/development.md 'build-rules home' 'OQ-18 designates docs/development.md as the rules home'

echo 'Checking the licence split and SPDX identifiers (design §2.4, §16.6, §17.1 D-19)...'
for doc in docs/architecture.md docs/development.md; do
	require_text "$doc" 'FSL-1.1-ALv2' 'registered SPDX identifier for the repository default licence'
	require_text "$doc" 'Apache-2.0' 'SPDX identifier for the agent and CLI'
	require_text "$doc" 'source-available' 'the non-agent code is source-available, not open source'
done
require_text docs/architecture.md 'agent/LICENSE' 'the agent subtree carries its own licence'
require_text docs/development.md 'SPDX-License-Identifier: Apache-2.0' 'per-file Go header rule'
for doc in docs/architecture.md docs/api.md docs/development.md docs/deployment.md; do
	forbid_text "$doc" 'FSL-1.1-Apache-2.0' 'not a registered SPDX identifier; SPDX tooling reports UNKNOWN'
	forbid_text "$doc" 'fully open-source' 'FSL is not an OSI-approved open-source licence'
done

echo 'Checking the authoritative root documents are still present and unreferenced as writable (design §0.3)...'
printf '%s\n' 'AI-Powered-DevOps-Platform-Complete-Technical-Research.md' 'PRD.md' 'Tech-Stack-Analysis.md' 'phases.md' | {
	while IFS= read -r doc; do
		[ -f "./$doc" ] || fail "authoritative document missing from the repository root: $doc"
	done
}
require_text docs/development.md 'read-only' 'the four reference documents are read-only inputs'

VIOLATIONS=$(wc -l <"$FAILFILE" | tr -d ' \t')
if [ "${VIOLATIONS:-0}" -ne 0 ]; then
	printf '\ndocumentation check failed with %s violation(s)\n' "$VIOLATIONS" >&2
	exit 1
fi

echo 'documentation check passed'
exit 0
