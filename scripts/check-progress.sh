#!/bin/sh
# Phase 0 PROGRESS.md structure check (design.md §15.3, §17, §18, Appendix E).
#
# Rejects:
#   * a missing or empty PROGRESS.md;
#   * a missing required section;
#   * a missing Phase 0 deliverable group (0.1 through 0.9) or the progress record row;
#   * a missing completion criterion (all 18 from Appendix E must appear with an
#     evidence column);
#   * an invalid task status (only done | in-progress | pending) or an invalid
#     phase status (only completed | in-progress | not-started | blocked);
#   * a missing decision row (D-1, D-2, D-5, D-14, D-19);
#   * the 0.9 label without its "Phase 0.5" alias (design §15.3).
#
# Read-only: it never creates, moves, formats or deletes anything.
#
# Portability notes: grep never combines -F with -i (MSYS grep 3.0 aborts on
# `-Fi`), and grep always reads a FILE rather than a pipe (`grep -q` on a pipe
# kills the upstream producer with SIGPIPE on some shells).

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

FILE=PROGRESS.md

FAILFILE=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-progress-$$")
LOWER=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-progress-lower-$$")
: >"$FAILFILE"
trap 'rm -f "$FAILFILE" "$LOWER"' EXIT HUP INT TERM

fail() {
	printf 'FAIL: %s\n' "$1" >&2
	printf 'x\n' >>"$FAILFILE"
}

ok() {
	printf 'ok:   %s\n' "$1"
}

if [ ! -f "$FILE" ]; then
	printf 'FAIL: %s is missing (design §18 requires it as a Phase 0 deliverable)\n' "$FILE" >&2
	exit 1
fi
if [ ! -s "$FILE" ]; then
	printf 'FAIL: %s is empty\n' "$FILE" >&2
	exit 1
fi

tr -d '\r' <"$FILE" | tr 'A-Z' 'a-z' >"$LOWER"

# has <literal, lower-case> -> 0 when present
has() {
	grep -F -- "$1" "$LOWER" >/dev/null 2>&1
}

# ── Required sections (design §18) ───────────────────────────────────────────
echo 'Checking required sections (design §18)...'
REQUIRED_SECTIONS='# progress
**current phase:**
**last updated:**
## phase status
## current phase task list — phase 0
## completion criteria — phase 0
## open questions requiring a decision
## decision log'

printf '%s\n' "$REQUIRED_SECTIONS" | {
	while IFS= read -r section; do
		[ -n "$section" ] || continue
		if has "$section"; then
			ok "section present: $section"
		else
			fail "required section is missing: $section (design §18)"
		fi
	done
}

# ── Phase rows: all six phases must appear ───────────────────────────────────
echo 'Checking the phase status table lists phases 0 through 5...'
for phase in 0 1 2 3 4 5; do
	if grep -E "^\| $phase \|" "$LOWER" >/dev/null 2>&1; then
		ok "phase row present: $phase"
	else
		fail "phase status table is missing a row for phase $phase (design §18)"
	fi
done

# ── Deliverable coverage: every Phase 0 group needs at least one row ─────────
echo 'Checking every Phase 0 deliverable group 0.1-0.9 has at least one task row...'
for group in 0.1 0.2 0.3 0.4 0.5 0.6 0.7 0.8 0.9; do
	if grep -E "^\| $group \|" "$LOWER" >/dev/null 2>&1; then
		ok "deliverable group covered: $group"
	else
		fail "no task row for Phase 0 deliverable group $group (design §18)"
	fi
done
if has 'progress record'; then
	ok 'deliverable group covered: progress record'
else
	fail 'no task row for the progress-record deliverable (design §18)'
fi

# 0.9 must carry its dependency-graph alias (design §15.3).
if has '0.5'; then
	if has 'phase 0.5'; then
		ok '0.9 is labelled with its "Phase 0.5" dependency-graph alias'
	else
		fail '0.9 must be labelled with its "Phase 0.5" alias (design §15.3)'
	fi
else
	fail '0.9 must be labelled with its "Phase 0.5" alias (design §15.3)'
fi

# ── Completion criteria: all 18 must appear with an evidence column ──────────
echo 'Checking all 18 completion criteria appear with an evidence column (Appendix E)...'
CRITERIA='make build
make test
make lint
docker-compose up
health check endpoint returns 200
frontend loads at localhost:3000
amd64+arm64
goreleaser produces signed
pre-commit hooks pass on all files
tools/list
create → poll → cancel
issuer validation blocks unauthorized
plan analyzer returns results
hnsw index
cyclonedx sbom
cosign keyless signing
fallback cascade functions end-to-end
circuit breaker trips'

printf '%s\n' "$CRITERIA" | {
	while IFS= read -r criterion; do
		[ -n "$criterion" ] || continue
		if has "$criterion"; then
			ok "criterion present: $criterion"
		else
			fail "completion criterion is missing from PROGRESS.md: $criterion (Appendix E)"
		fi
	done
}

# Every numbered criterion row 1..18 must exist and carry three columns.
CRIT_ROWS=$(grep -cE '^\| [0-9]+\. ' "$LOWER" 2>/dev/null || printf '0')
if [ "$CRIT_ROWS" -eq 18 ]; then
	ok 'exactly 18 numbered completion-criteria rows'
else
	fail "expected 18 numbered completion-criteria rows, found $CRIT_ROWS (Appendix E)"
fi
MISSING_EVIDENCE=$(grep -E '^\| [0-9]+\. ' "$LOWER" | awk -F'|' 'NF < 5 { print $2 }')
if [ -z "$MISSING_EVIDENCE" ]; then
	ok 'every completion criterion has a status and an evidence column'
else
	fail "completion criterion row without status/evidence columns: $MISSING_EVIDENCE"
fi

# ── Status vocabularies (design §18) ─────────────────────────────────────────
echo 'Checking the status vocabularies (design §18)...'
BAD_PHASE_STATUS=$(grep -E '^\| [0-9] \| ' "$LOWER" |
	awk -F'|' '{ s=$4; gsub(/^[ \t]+|[ \t]+$/, "", s);
		if (s != "completed" && s != "in-progress" && s != "not-started" && s != "blocked") print s }')
if [ -z "$BAD_PHASE_STATUS" ]; then
	ok 'every phase status is completed | in-progress | not-started | blocked'
else
	fail "invalid phase status(es): $BAD_PHASE_STATUS (design §18 allows exactly four)"
fi

BAD_TASK_STATUS=$(grep -E '^\| (0\.[1-9]|progress record) \|' "$LOWER" |
	awk -F'|' '{ s=$5; gsub(/^[ \t]+|[ \t]+$/, "", s);
		if (s != "done" && s != "in-progress" && s != "pending") print s }')
if [ -z "$BAD_TASK_STATUS" ]; then
	ok 'every task status is done | in-progress | pending'
else
	fail "invalid task status(es): $BAD_TASK_STATUS (design §18 allows exactly three)"
fi

BAD_CRIT_STATUS=$(grep -E '^\| [0-9]+\. ' "$LOWER" |
	awk -F'|' '{ s=$3; gsub(/^[ \t]+|[ \t]+$/, "", s);
		if (s != "done" && s != "in-progress" && s != "pending") print s }')
if [ -z "$BAD_CRIT_STATUS" ]; then
	ok 'every completion-criterion status is done | in-progress | pending'
else
	fail "invalid completion-criterion status(es): $BAD_CRIT_STATUS (design §18)"
fi

# ── Decision log rows (design §17.1) ────────────────────────────────────────
echo 'Checking the decision log carries the five settled decisions (design §17.1)...'
for decision in d-1 d-2 d-5 d-14 d-19; do
	if has "$decision —" || has "$decision -"; then
		ok "decision row present: $decision"
	else
		fail "decision log is missing a row for $decision (design §17.1)"
	fi
done

# ── Wording discipline inherited from D-19 ──────────────────────────────────
echo 'Checking licence wording discipline (design §2.4, §17.1 D-19)...'
if has 'fsl-1.1-apache-2.0'; then
	fail 'PROGRESS.md must not use the unregistered alias FSL-1.1-Apache-2.0 (design §2.4)'
else
	ok 'no unregistered FSL alias'
fi

VIOLATIONS=$(wc -l <"$FAILFILE" | tr -d ' \t')
if [ "${VIOLATIONS:-0}" -ne 0 ]; then
	printf '\nPROGRESS.md structure check failed with %s violation(s)\n' "$VIOLATIONS" >&2
	exit 1
fi

printf '\nPROGRESS.md structure check passed\n'
exit 0
