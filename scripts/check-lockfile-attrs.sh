#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/check-lockfile-attrs.sh — no lockfile may be marked `-diff` (design.md
# §0.5, §8.5, §16.5).
#
# Why
# ---
# `-diff` tells git to treat a file as binary, so `git diff` prints "Binary files
# differ" and the change cannot be reviewed at all. A lockfile diff is the
# highest-signal artifact in a dependency bump: it is where a new transitive package,
# a moved version or a changed hash becomes visible. Phase 0 set `-diff` on all four
# lockfiles, which is a review-integrity problem rather than cosmetics.
#
# `linguist-generated` is the correct marker and is required instead: it collapses the
# diff by default in GitHub's UI and excludes the file from language statistics, while
# leaving the content reviewable on request.
#
# The check queries `git check-attr`, so it tests the attributes git ACTUALLY applies
# — including any inherited from a nested .gitattributes — rather than grepping the
# file and hoping the two agree.
#
# Failure is exit 1 naming each offending path. Exit 1 also when no lockfile is found,
# so a rename cannot make the check trivially pass.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Every committed lockfile, plus grammars.lock.json which task 11.1 adds. A path that
# does not exist yet is skipped; the count check below still guards against the set
# becoming empty.
CANDIDATES='
frontend/pnpm-lock.yaml
backend/requirements.lock
backend/requirements-dev.lock
scripts/requirements-tools.lock
agent/go.sum
agent/tools/go.sum
agent/internal/scanner/grammars/grammars.lock.json
'

FOUND=0
FAILED=0

for path in $CANDIDATES; do
	[ -f "$path" ] || continue
	FOUND=$((FOUND + 1))

	diff_attr="$(git check-attr diff -- "$path" | sed 's/.*: //')"
	generated_attr="$(git check-attr linguist-generated -- "$path" | sed 's/.*: //')"

	if [ "$diff_attr" = "unset" ]; then
		printf 'ERROR: %s is marked `-diff`, so its changes cannot be reviewed\n' "$path" >&2
		FAILED=1
	else
		printf 'ok:   %-42s diff=%s generated=%s\n' "$path" "$diff_attr" "$generated_attr"
	fi

	if [ "$generated_attr" != "set" ]; then
		printf 'ERROR: %s should be marked `linguist-generated`\n' "$path" >&2
		FAILED=1
	fi
done

if [ "$FOUND" -eq 0 ]; then
	printf 'ERROR: no lockfile was found; the check would pass vacuously\n' >&2
	exit 1
fi

if [ "$FAILED" -ne 0 ]; then
	printf '\ncheck-lockfile-attrs: FAILED. Fix .gitattributes: keep `linguist-generated`, drop `-diff`.\n' >&2
	exit 1
fi

printf 'check-lockfile-attrs: %d lockfile(s), all reviewable and marked generated\n' "$FOUND"
