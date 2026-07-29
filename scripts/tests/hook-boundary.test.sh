#!/bin/sh
# Phase 0 hook-boundary test (design.md §0.3, §8.4, §14.1; completion criterion 9).
#
# Proves the two halves of the pre-commit contract behaviourally, by running the
# real hooks rather than only reading the configuration:
#
#   1. IMMUTABILITY — after `pre-commit run --all-files` executes every mutating
#      hook (prettier, end-of-file-fixer, trailing-whitespace), the four
#      authoritative reference documents are byte-identical. Their SHA-256 sums
#      are captured before and compared after.
#
#   2. GITLEAKS STILL SEES EVERYTHING — the gitleaks hook is not filtered by the
#      top-level exclusion. A positive control proves the scan is real and not a
#      no-op: a throwaway file containing a synthetic credential is staged, the
#      gitleaks hook is run, and the hook MUST fail. The upstream hook scans
#      staged content (`gitleaks git --pre-commit --redact --staged`), so staging
#      is what puts content in front of it.
#
# The positive control never touches a reference document, is removed from the
# index and the worktree on every exit path, and is never committed.
#
# Requires: pre-commit on PATH, a git worktree. Skips with a clear message when
# either is unavailable, so it never reports a false pass.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT" || exit 1

FAILURES=0

pass() { printf 'ok   - %s\n' "$1"; }
fail() {
	printf 'FAIL - %s\n' "$1" >&2
	FAILURES=$((FAILURES + 1))
}

AUTHORITATIVE_DOCS='AI-Powered-DevOps-Platform-Complete-Technical-Research.md
PRD.md
Tech-Stack-Analysis.md
phases.md'

CONTROL_FILE='.forgeops-gitleaks-positive-control.tmp'
WORK=$(mktemp -d 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-hookcheck-$$")
mkdir -p "$WORK" 2>/dev/null || true

cleanup() {
	# The control file must never survive, staged or unstaged.
	if [ -n "${CONTROL_FILE:-}" ]; then
		git rm --cached --quiet --force -- "$CONTROL_FILE" >/dev/null 2>&1 || true
		rm -f -- "$CONTROL_FILE"
	fi
	rm -rf "$WORK"
}
trap cleanup EXIT HUP INT TERM

if ! command -v pre-commit >/dev/null 2>&1; then
	printf 'SKIP - pre-commit is not installed; cannot exercise the hooks\n' >&2
	exit 1
fi
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	printf 'SKIP - not inside a git worktree; pre-commit needs one\n' >&2
	exit 1
fi

# The hooks must only ever see this project. A pre-commit run whose repository
# root is an ancestor directory would rewrite unrelated files, so refuse to run.
#
# The two paths are compared after resolving each through `cd ... && pwd`,
# because git on Windows prints `C:/path` while an MSYS shell's `pwd` prints
# `/c/path`; comparing the raw strings would fail on a correct setup.
TOPLEVEL_RAW=$(git rev-parse --show-toplevel 2>/dev/null || printf '')
TOPLEVEL=$(CDPATH= cd -- "$TOPLEVEL_RAW" 2>/dev/null && pwd)
if [ "$TOPLEVEL" != "$ROOT" ]; then
	fail "git toplevel ($TOPLEVEL_RAW -> $TOPLEVEL) is not the project root ($ROOT); refusing to run repository-wide hooks"
	printf '\nhook-boundary test FAILED (%s failing assertion(s))\n' "$FAILURES" >&2
	exit 1
fi
pass 'git toplevel is the project root, so hook blast radius is confined to ForgeOps'

# hash_of <file>
hash_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | cut -d' ' -f1
	else
		git hash-object "$1"
	fi
}

echo '# recording reference-document digests before running the mutating hooks'
printf '%s\n' "$AUTHORITATIVE_DOCS" | {
	while IFS= read -r doc; do
		[ -n "$doc" ] || continue
		if [ ! -f "$doc" ]; then
			fail "authoritative document is missing: $doc"
			continue
		fi
		hash_of "$doc" >"$WORK/$(printf '%s' "$doc" | tr -c 'A-Za-z0-9' '_').before"
	done
}

echo '# running every hook over all files (this executes the mutating hooks)'
pre-commit run --all-files >"$WORK/precommit.log" 2>&1
PRECOMMIT_STATUS=$?
# A non-zero status is acceptable: pre-commit reports failure when a formatter
# rewrites a project-owned file. What matters is WHICH files it rewrote.
printf 'note: pre-commit exited %s (non-zero merely means a hook reformatted something)\n' "$PRECOMMIT_STATUS"

echo '# asserting the four authoritative documents are byte-identical'
printf '%s\n' "$AUTHORITATIVE_DOCS" | {
	while IFS= read -r doc; do
		[ -n "$doc" ] || continue
		key=$(printf '%s' "$doc" | tr -c 'A-Za-z0-9' '_')
		[ -f "$WORK/$key.before" ] || continue
		before=$(cat "$WORK/$key.before")
		after=$(hash_of "$doc")
		if [ "$before" = "$after" ]; then
			pass "unchanged by every mutating hook: $doc"
		else
			fail "a mutating hook rewrote a read-only reference document: $doc"
		fi
	done
}

echo '# asserting gitleaks actually ran and was not skipped'
if grep -F 'gitleaks' "$WORK/precommit.log" >/dev/null 2>&1; then
	if grep -F 'gitleaks' "$WORK/precommit.log" | grep -F 'Skipped' >/dev/null 2>&1; then
		fail 'gitleaks was Skipped; it must always run (design §8.4, §14.1)'
	else
		pass 'gitleaks ran over the repository (not skipped by the four-document exclusion)'
	fi
else
	fail 'gitleaks does not appear in the pre-commit output at all'
fi

echo '# positive control: a staged synthetic credential must make gitleaks fail'
# Assembled at runtime so this test file itself carries no scannable secret.
# A synthetic private-key block is used deliberately: gitleaks allowlists the
# well-known AWS documentation keys (AKIAIOSFODNN7EXAMPLE and friends), so those
# would produce a false "no findings" result and the control would prove nothing.
{
	printf '%s\n' 'positive control for scripts/tests/hook-boundary.test.sh'
	printf '%s%s%s\n' '-----BEGIN ' 'RSA PRIVATE' ' KEY-----'
	printf '%s\n' 'MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu'
	printf '%s\n' 'KUpRKfFLfRYC9AIKjbJTWit+CqvjWYzvQwECAwEAAQ=='
	printf '%s%s%s\n' '-----END ' 'RSA PRIVATE' ' KEY-----'
} >"$CONTROL_FILE"

if git add --force -- "$CONTROL_FILE" >/dev/null 2>&1; then
	pre-commit run gitleaks >"$WORK/gitleaks.log" 2>&1
	GITLEAKS_STATUS=$?
	if [ "$GITLEAKS_STATUS" -ne 0 ]; then
		pass 'gitleaks detected a staged synthetic credential, so the scan is real'
	else
		fail 'gitleaks passed on a staged synthetic credential; the secret gate is not effective'
	fi
	git rm --cached --quiet --force -- "$CONTROL_FILE" >/dev/null 2>&1 || true
else
	fail "could not stage the positive-control file $CONTROL_FILE"
fi
rm -f -- "$CONTROL_FILE"

if [ -e "$CONTROL_FILE" ]; then
	fail "positive-control file survived cleanup: $CONTROL_FILE"
else
	pass 'positive-control file removed from the worktree and the index'
fi

echo '# asserting no reference document is staged or left modified by this test'
printf '%s\n' "$AUTHORITATIVE_DOCS" | {
	while IFS= read -r doc; do
		[ -n "$doc" ] || continue
		if git status --porcelain -- "$doc" 2>/dev/null | grep -q .; then
			fail "reference document has uncommitted modifications after the hook run: $doc"
		fi
	done
}

printf '\n'
if [ "$FAILURES" -ne 0 ]; then
	printf 'hook-boundary test FAILED (%s failing assertion(s))\n' "$FAILURES" >&2
	exit 1
fi
echo 'hook-boundary test passed'
exit 0
