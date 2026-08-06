#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/check-lock-freshness.sh — regenerate both backend lockfiles in an
# isolated copy and fail if either committed lock differs (design.md §7.7, §8.3).
#
# Why an isolated COPY of the inputs rather than a bare temp output path:
# pip-compile records its own invocation in the generated header, including the
# --output-file value. Writing to /tmp/xxxx/requirements.lock would therefore
# always differ from the committed file in that one comment line and report a
# false staleness. The check instead copies pyproject.toml into a temp directory
# and regenerates with the SAME relative output filename, so a clean tree
# produces byte-identical output including the header.
#
# Why the EXISTING locks are seeded into the temp directory too:
# pip-compile honours the pins already present in its output file and only moves
# them when the inputs force it (that is what makes `--upgrade` a separate flag).
# If the temp directory contained pyproject.toml alone, every run would re-resolve
# each transitive to the newest compatible release, so the gate would turn red the
# moment any transitive dependency published — unrelated to whether the committed
# lock is actually stale. That was observed directly: two runs minutes apart
# disagreed only because `annotated-doc` released 0.0.5 in between.
#
# Seeding makes the question the right one: "does the committed lock still satisfy
# pyproject.toml?" rather than "has anything anywhere been released since?".
# Deliberate upgrades remain possible and explicit via `make lock-backend` with
# pip-compile's --upgrade, which is never used by this check.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

# shellcheck source=scripts/lib/pip-compile.sh
. "$SCRIPT_DIR/lib/pip-compile.sh"
resolve_pip_compile "$REPO_ROOT"

TMPDIR_LOCK="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_LOCK"' EXIT

cp "$BACKEND_DIR/pyproject.toml" "$TMPDIR_LOCK/pyproject.toml"

# Seed the current pins so pip-compile re-resolves only what pyproject.toml forces
# (see the header note). A missing lock is fine — that is genuine staleness and the
# diff below will report it.
for lock in requirements.lock requirements-dev.lock; do
	if [ -f "$BACKEND_DIR/$lock" ]; then
		cp "$BACKEND_DIR/$lock" "$TMPDIR_LOCK/$lock"
	fi
done

printf '==> check-lock-freshness: regenerating both locks in an isolated copy\n'

cd "$TMPDIR_LOCK"

run_pip_compile \
	--generate-hashes \
	--reuse-hashes \
	--allow-unsafe \
	--strip-extras \
	--output-file=requirements.lock \
	pyproject.toml

run_pip_compile \
	--generate-hashes \
	--reuse-hashes \
	--allow-unsafe \
	--strip-extras \
	--extra=dev \
	--output-file=requirements-dev.lock \
	pyproject.toml



cd "$REPO_ROOT"

FAILED=0

for lock in requirements.lock requirements-dev.lock; do
	# Committed files may be checked out CRLF on Windows; compare content, not
	# line-ending policy, so a correct lock is never reported stale.
	if diff -u --strip-trailing-cr "$BACKEND_DIR/$lock" "$TMPDIR_LOCK/$lock" >/dev/null 2>&1; then
		printf 'ok:   %s is up to date\n' "$lock"
	else
		printf 'ERROR: backend/%s is stale. Run: make lock-backend\n' "$lock"
		diff -u --strip-trailing-cr "$BACKEND_DIR/$lock" "$TMPDIR_LOCK/$lock" || true
		FAILED=1
	fi



done

if [ "$FAILED" -eq 1 ]; then
	exit 1
fi

printf 'check-lock-freshness: both lockfiles are up to date\n'
