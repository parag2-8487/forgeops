#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# Negative controls for scripts/check-structure.sh (design §0.4.5's rule applied to a
# repository check rather than to a property: a check whose own failure modes are
# untested is a check nobody can trust).
#
# Three controls:
#   1. the real tree passes;
#   2. a structural list that resolves to no existing directory FAILS — the vacuity
#      trap, which would otherwise make the whole section pass while asserting nothing;
#   3. a committed .go file inside a structural-only directory FAILS — so the section
#      is proven to still have teeth after control 2 proved it cannot be emptied.
#
# The mutated copy is written into scripts/ rather than into a temp directory, because
# check-structure.sh derives the repository root from `dirname $0/..`. A copy run from
# /tmp would check an empty tree and "fail" for reasons that have nothing to do with
# the mutation — which is exactly the kind of control that proves nothing.
set -uo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT" || exit 1

CONTROL="scripts/.check-structure.control.$$.sh"
STRAY=""
cleanup() {
	rm -f "$CONTROL"
	[ -n "$STRAY" ] && rm -f "$STRAY"
	return 0
}
trap cleanup EXIT HUP INT TERM

failures=0
pass() { printf 'ok:   %s\n' "$1"; }
bad() {
	printf 'FAIL: %s\n' "$1" >&2
	failures=$((failures + 1))
}

# ── Control 1: the real tree passes ─────────────────────────────────────────────
if bash scripts/check-structure.sh >/dev/null 2>&1; then
	pass 'control 1: check-structure.sh passes on the real tree'
else
	bad 'control 1: check-structure.sh does not pass on the real tree'
fi

# ── Control 2: an empty structural list is reported vacuous ─────────────────────
sed -e "s|^GO_STRUCTURAL_DIRS='agent/pkg'|GO_STRUCTURAL_DIRS='agent/no-such-dir'|" \
	-e "s|^backend/src/monitoring$|backend/src/no-such-b|" \
	-e "s|^backend/src/incidents$|backend/src/no-such-c|" \
	-e "s|^backend/src/notifications'$|backend/src/no-such-d'|" \
	-e "s|^PY_STRUCTURAL_DIRS='backend/src/deployment|PY_STRUCTURAL_DIRS='backend/src/no-such-a|" \
	scripts/check-structure.sh >"$CONTROL"

if grep -q "agent/no-such-dir" "$CONTROL" &&
	grep -q "backend/src/no-such-a" "$CONTROL" &&
	grep -q "backend/src/no-such-b" "$CONTROL" &&
	grep -q "backend/src/no-such-c" "$CONTROL" &&
	grep -q "backend/src/no-such-d" "$CONTROL"; then
	pass 'control 2: the mutation applied to both lists in full'
else
	bad 'control 2: the mutation did not apply; the control would be a no-op'
fi

out=$(bash "$CONTROL" 2>&1)
if printf '%s' "$out" | grep -q 'GO_STRUCTURAL_DIRS resolves to nothing'; then
	pass 'control 2: an empty Go structural list is reported vacuous'
else
	bad 'control 2: an empty Go structural list was NOT reported vacuous'
fi
if printf '%s' "$out" | grep -q 'PY_STRUCTURAL_DIRS resolves to nothing'; then
	pass 'control 2: an empty backend structural list is reported vacuous'
else
	bad 'control 2: an empty backend structural list was NOT reported vacuous'
fi
rm -f "$CONTROL"

# ── Control 3: a committed .go file in a structural-only directory is caught ────
# `git add -N` records the path in the index without staging content, which is what
# `git ls-files` reads. Nothing is committed and the file is removed on exit.
STRAY="agent/pkg/stray_control_$$.go"
printf 'package pkg\n' >"$STRAY"
git add -N -- "$STRAY" >/dev/null 2>&1
out=$(bash scripts/check-structure.sh 2>&1)
if printf '%s' "$out" | grep -q "structural-only Go directory must contain no .go file"; then
	pass 'control 3: a committed .go file in a structural-only directory is caught'
else
	bad 'control 3: a committed .go file in a structural-only directory was NOT caught'
fi
git rm --cached --quiet -- "$STRAY" >/dev/null 2>&1
rm -f "$STRAY"
STRAY=""

# ── Control 4: an untracked build artifact is NOT a finding ────────────────────
mkdir -p agent/pkg/__ignored_control__
printf 'x\n' >agent/pkg/__ignored_control__/artifact.go
out=$(bash scripts/check-structure.sh 2>&1)
rm -rf agent/pkg/__ignored_control__
if printf '%s' "$out" | grep -q '__ignored_control__'; then
	bad 'control 4: an UNTRACKED artifact produced a finding; the check reports noise'
else
	pass 'control 4: an untracked build artifact produces no finding'
fi

if [ "$failures" -ne 0 ]; then
	printf '\ncheck-structure controls failed: %s\n' "$failures" >&2
	exit 1
fi
printf '\ncheck-structure controls passed\n'
exit 0
