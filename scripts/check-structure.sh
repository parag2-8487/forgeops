#!/bin/sh
# Phase 0 repository-structure check.
#
# Enforces design.md §0.3 (workspace placement, read-only reference documents),
# §1.3 (structural artifacts must never become importable placeholders) and
# §2.3 (monorepo layout, no nested project directory).
#
# Read-only: it never creates, moves, formats or deletes anything. It prints
# every violation it finds and exits non-zero if there is at least one.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

FAILFILE=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-structure-$$")
: >"$FAILFILE"
trap 'rm -f "$FAILFILE"' EXIT HUP INT TERM

fail() {
	printf 'FAIL: %s\n' "$1" >&2
	printf 'x\n' >>"$FAILFILE"
}

# Authoritative, read-only reference documents (design §0.3).
AUTHORITATIVE_DOCS='AI-Powered-DevOps-Platform-Complete-Technical-Research.md
PRD.md
Tech-Stack-Analysis.md
phases.md'

# Directories that must exist for the Phase 0 layout (design §2.3).
REQUIRED_DIRS='scripts
docs
policies
.github/workflows
agent
agent/cmd/agent
agent/internal/config
agent/internal/logging
agent/internal/app
agent/internal/connection
agent/internal/docker
agent/internal/k8s
agent/internal/scanner
agent/internal/fileops
agent/internal/iac
agent/internal/git
agent/internal/telemetry
agent/internal/mcp
agent/internal/executor
agent/internal/validator
agent/internal/policy
agent/internal/devtools
agent/pkg
agent/testdata
agent/testfixtures/tofu-null
backend
backend/alembic/versions
backend/src/core
backend/src/projects
backend/src/analysis
backend/src/analysis/plan_analyzer
backend/src/ai/routing
backend/src/ai/rate_limit
backend/src/ai/cache
backend/src/ai/keys
backend/src/mcp
backend/src/auth
backend/src/generation
backend/src/deployment
backend/src/monitoring
backend/src/incidents
backend/src/policies
backend/src/secrets
backend/src/notifications
backend/src/websocket
backend/tests/unit
backend/tests/integration
backend/tests/property
frontend
frontend/app
frontend/components/ui
frontend/components/layout
frontend/components/providers
frontend/features
frontend/lib/api
frontend/hooks
frontend/stores
frontend/e2e
frontend/load'

# Structural-only directories (design §1.3): marker files only, never code.
# Structural-only directories, narrowed for Phase 1.
#
# This list is the set of directories that are STILL markers — a phase owns them and
# that phase has not started. It shrank when Phase 1 began, and the shrinking is the
# point: a directory whose phase has arrived must be allowed to hold code, or the
# check reports a violation for doing exactly what the plan says.
#
# Moved out because Phase 1 owns and populates them:
#   agent/internal/executor   design §10.5, tasks 7.2 / 8.7
#   agent/internal/policy     design §10.6, tasks 9.4
#   agent/internal/validator  design §10.7, tasks 14.1-14.6
#   agent/internal/devtools   design §10.10, tasks 14.7
#   backend/src/auth          design §11.2, tasks 6.1-6.4
#   backend/src/generation    design §11.5, tasks 13.x
#   backend/src/policies      design §11.7, tasks 9.5
#   backend/src/secrets       design §11.8, tasks 10.4
#   backend/src/websocket     design §11.10, tasks 8.4
#
# What stays: `agent/pkg` (no phase claims it) and the four backend domains
# design §1.2 excludes from Phase 1 outright. `governance/`, `audit/`, `projects/`
# and `analysis/` were never on this list and are Phase 1 domains.
GO_STRUCTURAL_DIRS='agent/pkg'

PY_STRUCTURAL_DIRS='backend/src/deployment
backend/src/monitoring
backend/src/incidents
backend/src/notifications'

FE_STRUCTURAL_DIR='frontend/features'

# report_matches <message prefix> -- reads NUL-free paths on stdin, one per line
report_matches() {
	message=$1
	while IFS= read -r path; do
		[ -n "$path" ] || continue
		fail "$message: $path"
	done
}

echo 'Checking authoritative root documents (design §0.3)...'
printf '%s\n' "$AUTHORITATIVE_DOCS" | {
	while IFS= read -r doc; do
		[ -n "$doc" ] || continue
		if [ ! -f "./$doc" ]; then
			fail "authoritative document missing from the repository root: $doc"
		fi
		find . -mindepth 2 -type f -name "$doc" \
			-not -path './.git/*' -not -path './.kiro/*' 2>/dev/null |
			report_matches 'authoritative document found outside the repository root (moved or copied)'
	done
}

echo 'Checking that the monorepo root is the workspace root (design §0.3, §2.3)...'
printf '%s\n' 'ForgeOps' 'ai-devops-platform' | {
	while IFS= read -r nested; do
		if [ -d "./$nested" ]; then
			fail "nested project directory is forbidden; the repository root is the monorepo root: ./$nested"
		fi
	done
}

echo 'Checking required Phase 0 directories (design §2.3)...'
printf '%s\n' "$REQUIRED_DIRS" | {
	while IFS= read -r dir; do
		[ -n "$dir" ] || continue
		[ -d "./$dir" ] || fail "required directory is missing: $dir"
	done
}

echo 'Checking the validated public env surface (design §2.3 vs §12.1)...'
# design §2.3 draws `frontend/lib/{api,env}/` as directories, while the frontend
# low-level design §12.1 specifies `lib/env.ts` as a single zod-validated module.
# §12.1 is the more specific authority for the frontend's own layout, so either
# form satisfies the layout; what must never happen is the surface being absent.
if [ -d frontend/lib/env ] || [ -f frontend/lib/env.ts ]; then
	:
else
	fail 'the validated public env surface is missing: expected frontend/lib/env/ (design §2.3) or frontend/lib/env.ts (design §12.1)'
fi

echo 'Checking structural-only Go directories (design §1.3)...'
# Only TRACKED files are considered. §1.3 forbids a committed placeholder; a local
# build artifact — __pycache__, a compiled object, an editor swap file — is not one,
# and reporting it made this check produce findings a developer learns to ignore.
tracked_in() {
	git ls-files -- "$1" 2>/dev/null
}

STRUCTURAL_DIRS_SEEN=0
# count_existing <newline-separated dirs> — how many exist. Computed with no external
# command, and OUTSIDE the pipes below, because a variable set inside the right-hand
# side of a pipe lives in that subshell: the guard would then read the outer value and
# fire even when the list is fine. That is the vacuity trap arriving by a second route.
count_existing() {
	_n=0
	for _d in $1; do
		[ -d "$_d" ] && _n=$((_n + 1))
	done
	printf '%s' "$_n"
}
GO_DIRS_SEEN=$(count_existing "$GO_STRUCTURAL_DIRS")
PY_DIRS_SEEN=$(count_existing "$PY_STRUCTURAL_DIRS")
if [ "$GO_DIRS_SEEN" -eq 0 ]; then
	fail 'no structural-only Go directory exists; GO_STRUCTURAL_DIRS resolves to nothing and the check is vacuous'
fi
if [ "$PY_DIRS_SEEN" -eq 0 ]; then
	fail 'no deferred backend domain exists; PY_STRUCTURAL_DIRS resolves to nothing and the check is vacuous'
fi

printf '%s\n' "$GO_STRUCTURAL_DIRS" | {
	while IFS= read -r dir; do
		[ -n "$dir" ] || continue
		[ -d "$dir" ] || continue
		STRUCTURAL_DIRS_SEEN=1
		tracked_in "$dir" | grep -E '\.go$' |
			report_matches 'structural-only Go directory must contain no .go file (design §1.3)'
		tracked_in "$dir" | grep -vE '(^|/)(README\.md|\.gitkeep)$' |
			report_matches 'structural-only Go directory may contain only README.md or .gitkeep (design §1.3)'
	done
}

echo 'Checking deferred backend domains (design §1.3)...'
printf '%s\n' "$PY_STRUCTURAL_DIRS" | {
	while IFS= read -r dir; do
		[ -n "$dir" ] || continue
		[ -d "$dir" ] || continue
		tracked_in "$dir" | grep -E '\.py$' |
			report_matches 'deferred backend domain must contain no importable Python module (design §1.3)'
		tracked_in "$dir" | grep -vE '(^|/)(README\.md|\.gitkeep)$' |
			report_matches 'deferred backend domain may contain only README.md or .gitkeep (design §1.3)'
	done
}

echo 'Checking frontend/features (design §1.3)...'
if [ -d "$FE_STRUCTURAL_DIR" ]; then
	find "$FE_STRUCTURAL_DIR" -type f ! -name 'README.md' ! -name '.gitkeep' 2>/dev/null |
		report_matches 'frontend/features is structural only and must contain no feature placeholder (design §1.3)'
	find "$FE_STRUCTURAL_DIR" -mindepth 1 -type d 2>/dev/null |
		report_matches 'frontend/features must contain no feature subdirectory (design §1.3)'
fi

echo 'Checking for package-doc-only Go stubs (design §1.3)...'
if [ -d agent ]; then
	find agent -type f -name 'doc.go' 2>/dev/null |
		report_matches 'package-doc-only Go file is a forbidden stub (design §1.3)'
fi

VIOLATIONS=$(wc -l <"$FAILFILE" | tr -d ' \t')
if [ "${VIOLATIONS:-0}" -ne 0 ]; then
	printf '\nrepository-structure check failed with %s violation(s)\n' "$VIOLATIONS" >&2
	exit 1
fi

echo 'repository-structure check passed'
exit 0
