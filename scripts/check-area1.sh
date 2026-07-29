#!/bin/sh
# Phase 0 area-1 safeguard runner (task 1.8).
#
# Executes every repository-boundary safeguard added by tasks 1.1-1.7 and the
# behavioural hook-boundary test, then reports one aggregate result:
#
#   scripts/check-structure.sh          layout + structural-artifact discipline (§0.3, §1.3, §2.3)
#   scripts/check-licence.sh            root LICENSE, agent/LICENSE, agent/NOTICE, README (§2.4)
#   scripts/check-hygiene.sh            .gitignore + pre-commit hook configuration (§0.3, §8.4)
#   scripts/check-makefile.sh           initial Makefile contracts (§13.4)
#   scripts/check-docs.sh               docs/ content requirements (§4.2, §4.4, §14.2, §15.2)
#   scripts/check-progress.sh           PROGRESS.md structure and vocabularies (§18)
#   scripts/tests/init-env.test.sh      idempotent .env creation (§13.3)
#   scripts/tests/hook-boundary.test.sh reference docs immutable + gitleaks effective (§8.4, §14.1)
#
# Scope note: this runner validates only artifacts owned by area 1. Backend
# pyproject metadata (task 2.1), frontend package metadata (task 6.1) and Go
# SPDX headers (task 3.1) are deliberately out of scope here.
#
# Every check is read-only apart from hook-boundary.test.sh, which may let the
# mandated formatters rewrite project-owned files and which cleans up its own
# fixture.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

CHECKS='scripts/check-structure.sh
scripts/check-licence.sh
scripts/check-hygiene.sh
scripts/check-makefile.sh
scripts/check-docs.sh
scripts/check-progress.sh
scripts/tests/init-env.test.sh
scripts/tests/hook-boundary.test.sh'

FAILED=''
TOTAL=0
PASSED=0

printf '=== ForgeOps Phase 0 — area-1 safeguards ===\n\n'

printf '%s\n' "$CHECKS" | {
	while IFS= read -r check; do
		[ -n "$check" ] || continue
		printf -- '--- %s\n' "$check"
		if [ ! -f "$check" ]; then
			printf 'FAIL: %s is missing\n' "$check" >&2
			printf '%s\n' "$check" >>"$ROOT/.area1-failures.tmp"
			continue
		fi
		if sh "$check"; then
			printf 'RESULT: %s PASSED\n\n' "$check"
		else
			printf 'RESULT: %s FAILED\n\n' "$check" >&2
			printf '%s\n' "$check" >>"$ROOT/.area1-failures.tmp"
		fi
	done
}

TOTAL=$(printf '%s\n' "$CHECKS" | grep -c '[^[:space:]]')
if [ -f "$ROOT/.area1-failures.tmp" ]; then
	FAILED=$(cat "$ROOT/.area1-failures.tmp")
	rm -f "$ROOT/.area1-failures.tmp"
fi

if [ -n "$FAILED" ]; then
	COUNT=$(printf '%s\n' "$FAILED" | grep -c '[^[:space:]]')
	printf '=== area-1 safeguards FAILED: %s of %s checks ===\n' "$COUNT" "$TOTAL" >&2
	printf '%s\n' "$FAILED" | sed 's/^/  - /' >&2
	exit 1
fi

PASSED=$TOTAL
printf '=== area-1 safeguards passed: %s of %s checks ===\n' "$PASSED" "$TOTAL"
exit 0
