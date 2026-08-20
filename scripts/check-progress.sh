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
# Row matching is deliberately whitespace-tolerant: Prettier owns markdown
# formatting (design §8.4) and pads table cells to align columns, so a check
# that demanded exactly one space either side of a cell value would break the
# moment the mandated formatter ran.
echo 'Checking the phase status table lists phases 0 through 5...'
for phase in 0 1 2 3 4 5; do
	if grep -E "^\|[[:space:]]*$phase[[:space:]]*\|" "$LOWER" >/dev/null 2>&1; then
		ok "phase row present: $phase"
	else
		fail "phase status table is missing a row for phase $phase (design §18)"
	fi
done

# ── Deliverable coverage: every Phase 0 group needs at least one row ─────────
echo 'Checking every Phase 0 deliverable group 0.1-0.9 has at least one task row...'
for group in 0.1 0.2 0.3 0.4 0.5 0.6 0.7 0.8 0.9; do
	if grep -E "^\|[[:space:]]*$group[[:space:]]*\|" "$LOWER" >/dev/null 2>&1; then
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
CRIT_ROWS=$(grep -cE '^\|[[:space:]]*[0-9]+\. ' "$LOWER" 2>/dev/null || printf '0')
if [ "$CRIT_ROWS" -eq 18 ]; then
	ok 'exactly 18 numbered completion-criteria rows'
else
	fail "expected 18 numbered completion-criteria rows, found $CRIT_ROWS (Appendix E)"
fi
MISSING_EVIDENCE=$(grep -E '^\|[[:space:]]*[0-9]+\. ' "$LOWER" | awk -F'|' 'NF < 5 { print $2 }')
if [ -z "$MISSING_EVIDENCE" ]; then
	ok 'every completion criterion has a status and an evidence column'
else
	fail "completion criterion row without status/evidence columns: $MISSING_EVIDENCE"
fi

# ── Status vocabularies (design §18) ─────────────────────────────────────────
echo 'Checking the status vocabularies (design §18)...'
BAD_PHASE_STATUS=$(grep -E '^\|[[:space:]]*[0-9][[:space:]]*\|' "$LOWER" |
	awk -F'|' '{ s=$4; gsub(/^[ \t]+|[ \t]+$/, "", s);
		if (s != "completed" && s != "in-progress" && s != "not-started" && s != "blocked") print s }')
if [ -z "$BAD_PHASE_STATUS" ]; then
	ok 'every phase status is completed | in-progress | not-started | blocked'
else
	fail "invalid phase status(es): $BAD_PHASE_STATUS (design §18 allows exactly four)"
fi

BAD_TASK_STATUS=$(grep -E '^\|[[:space:]]*(0\.[1-9]|progress record)[[:space:]]*\|' "$LOWER" |
	awk -F'|' '{ s=$5; gsub(/^[ \t]+|[ \t]+$/, "", s);
		if (s != "done" && s != "in-progress" && s != "pending") print s }')
if [ -z "$BAD_TASK_STATUS" ]; then
	ok 'every task status is done | in-progress | pending'
else
	fail "invalid task status(es): $BAD_TASK_STATUS (design §18 allows exactly three)"
fi

BAD_CRIT_STATUS=$(grep -E '^\|[[:space:]]*[0-9]+\. ' "$LOWER" |
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

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 (design §18, Appendix B, Appendix E; task 20.15)
#
# Everything above this line reads Phase 0 and only Phase 0. That was the whole defect:
# `BAD_TASK_STATUS` greps `^\| 0\.[1-9]` and `progress record`, so 90 Phase 1 rows carried
# the status `complete` -- a value this file's own header says does not exist -- and the
# check passed. A gate blind to the phase it is asked about is pattern B, and task 20.15
# names extending this script as its own deliverable, which had not been done.
#
# Task 20.15 requires: every deliverable §1.1-§1.11, all fourteen criteria with non-empty
# evidence, all thirty-one properties with a location AND a control, every decision row, and
# Phase 1 marked `completed` only when all of it holds.
# ─────────────────────────────────────────────────────────────────────────────
echo 'Checking the Phase 1 record (design Appendix B, Appendix E; task 20.15)...'

PHASE1_SECTIONS='## current phase task list — phase 1
## completion criteria — phase 1
## deliverable coverage — phase 1
## property test coverage — q-01 to q-31'

printf '%s\n' "$PHASE1_SECTIONS" | {
	while IFS= read -r section; do
		[ -n "$section" ] || continue
		if has "$section"; then
			ok "section present: $section"
		else
			fail "required Phase 1 section is missing: $section (task 20.15)"
		fi
	done
}

# ── Phase 1 task statuses: the blind spot ───────────────────────────────────
# Any leaf id whose group is 1..20. Phase 0 ids are `0.x` and are excluded by the pattern,
# so this adds a check rather than duplicating one.
#
# Three guards, each earned. `NF == 6` keeps the four-column status table and drops the
# three-column evidence table, whose prose contains `|` characters and therefore splits into
# far more fields. The length and space guards are belt-and-braces for the same reason: a
# status is a single short token, so anything long or containing a space is prose that
# happened to land in field 5, and reporting it produces a screenful of noise in place of the
# one-word answer -- which is what the first version of this check did.
BAD_P1_STATUS=$(grep -E '^\|[[:space:]]*(1?[0-9]|20)\.[0-9]+[[:space:]]*\|' "$LOWER" |
	awk -F'|' 'NF == 6 { s=$5; gsub(/^[ \t]+|[ \t]+$/, "", s);
		if (s != "" && length(s) <= 15 && s !~ / / &&
		    s != "done" && s != "in-progress" && s != "pending" && s != "blocked") print s }' |
	sort -u)
if [ -z "$BAD_P1_STATUS" ]; then
	ok 'every Phase 1 leaf status is done | in-progress | pending | blocked'
else
	fail "invalid Phase 1 leaf status(es): $BAD_P1_STATUS (design §18 allows exactly these)"
fi

# ── Deliverables §1.1 – §1.11 ───────────────────────────────────────────────
# Scoped to the deliverable-coverage section. An earlier version matched `^| 1.9 |` anywhere
# in the file, which silently answered the question with GROUP 1's leaf rows: 1.1 through 1.8
# "passed" because leaves of that number exist, and 1.9 through 1.11 failed because no leaf
# has that number. It was checking the wrong table and agreeing with itself for eight rows.
P1_DELIV=$(awk '
	tolower($0) ~ /^## deliverable coverage — phase 1/ { inside = 1; next }
	inside && /^## / { inside = 0 }
	inside { print }
' "$LOWER")

for d in 1 2 3 4 5 6 7 8 9 10 11; do
	if printf '%s\n' "$P1_DELIV" | grep -E "^\|[[:space:]]*\*?\*?1\.$d\*?\*?[[:space:]]*\|" >/dev/null 2>&1; then
		ok "Phase 1 deliverable covered: 1.$d"
	else
		fail "Phase 1 deliverable 1.$d has no coverage row (task 20.15, Appendix E)"
	fi
done

# ── The fourteen completion criteria, each with non-empty evidence ──────────
# Rows are `| C<n> | <criterion> | <status> | <evidence> |`. The `C` prefix is load-bearing:
# a bare number collides with the phase status table (`| 0 | … | completed |` through
# `| 5 | … |`), and the phase-status vocabulary check above greps any row whose first cell is
# a lone digit -- so criteria 1 through 5 were read as PHASE rows and their `done` status was
# reported as an invalid phase status.
P1_CRIT=$(awk '
	tolower($0) ~ /^## completion criteria — phase 1/ { inside = 1; next }
	inside && /^## / { inside = 0 }
	inside && /^\|[[:space:]]*[Cc][0-9]+[[:space:]]*\|/ { print }
' "$FILE")

P1_CRIT_COUNT=$(printf '%s\n' "$P1_CRIT" | grep -c '^|' 2>/dev/null || printf '0')
if [ "$P1_CRIT_COUNT" -eq 14 ]; then
	ok 'exactly 14 numbered Phase 1 completion-criteria rows'
else
	fail "expected 14 Phase 1 completion-criteria rows, found $P1_CRIT_COUNT (Appendix E)"
fi

for n in 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
	row=$(printf '%s\n' "$P1_CRIT" | awk -F'|' -v want="c$n" '{
		id = $2; gsub(/^[ \t]+|[ \t]+$/, "", id)
		if (tolower(id) == want) print $0
	}')
	if [ -z "$row" ]; then
		fail "Phase 1 completion criterion $n is missing (Appendix E)"
		continue
	fi
	status=$(printf '%s\n' "$row" | awk -F'|' '{ s=$4; gsub(/^[ \t]+|[ \t]+$/, "", s); print tolower(s) }')
	evidence=$(printf '%s\n' "$row" | awk -F'|' '{ e=$5; gsub(/^[ \t]+|[ \t]+$/, "", e); print e }')
	if [ -z "$evidence" ]; then
		fail "Phase 1 criterion $n has an empty evidence column (task 20.15)"
	elif [ "$status" != "done" ] && [ "$status" != "in-progress" ] && [ "$status" != "pending" ]; then
		fail "Phase 1 criterion $n has status '$status' (design §18 allows three)"
	else
		ok "Phase 1 criterion $n carries a status and evidence"
	fi
done

# ── Q-01 … Q-31: a location AND a negative control, per property ────────────
# Appendix B's rule is that a property which cannot fail is not a property, so a row with a
# location but no control is reported. `scripts/check-mutation-manifest.py` enforces the same
# thing against the manifest; this enforces that the RECORD does not overstate it.
P1_PROPS=$(awk '
	tolower($0) ~ /^## property test coverage — q-01 to q-31/ { inside = 1; next }
	inside && /^## / { inside = 0 }
	inside && /^\|[[:space:]]*\*?\*?[Qq]-[0-9][0-9]/ { print }
' "$FILE")

for n in 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31; do
	row=$(printf '%s\n' "$P1_PROPS" | grep -E "^\|[[:space:]]*\*?\*?[Qq]-$n\b" | head -n 1)
	if [ -z "$row" ]; then
		fail "property Q-$n has no coverage row (Appendix B declares 31, task 20.15)"
		continue
	fi
	location=$(printf '%s\n' "$row" | awk -F'|' '{ v=$3; gsub(/^[ \t]+|[ \t]+$/, "", v); print v }')
	control=$(printf '%s\n' "$row" | awk -F'|' '{ v=$4; gsub(/^[ \t]+|[ \t]+$/, "", v); print tolower(v) }')
	# A placeholder is not a control. Without this clause the row `| Q-06 | ... | none | ...`
	# satisfies "non-empty" and the gate certifies an uncontrolled property -- which is the
	# same class of defect as the counter that said 31, one column over. The `*absent*` and
	# `*no manifest row*` globs are here because the honest wording this record uses to
	# DECLARE the gap would otherwise pass the check that measures it.
	case "$control" in
	'' | none | n/a | na | tbd | todo | outstanding | missing | pending | '—' | '-' | '–' | \
		'none yet' | 'not yet' | 'to do' | 'not written' | 'no control')
		control='' ;;
	*absent* | *'no manifest row'* | *'no control'* | *'not registered'* | *'to be written'* | \
		*blocked* | *'does not test'* | *'imports no production code'* | *unreachable* | \
		*'cannot be failed'* | *'never invoked'* | *tautolog*)
		control='' ;;
	esac
	if [ -z "$location" ]; then
		fail "property Q-$n has no location (task 20.15)"
	elif [ -z "$control" ]; then
		fail "property Q-$n has no negative control (Appendix B: a property that cannot fail is not one)"
	else
		ok "property Q-$n has a location and a control"
	fi
done

# ── Decisions D-28 … D-50 ───────────────────────────────────────────────────
MISSING_DECISIONS=''
for n in 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50; do
	if has "d-$n —" || has "d-$n -" || has "d-$n |"; then
		:
	else
		MISSING_DECISIONS="$MISSING_DECISIONS d-$n"
	fi
done
if [ -z "$MISSING_DECISIONS" ]; then
	ok 'decision rows D-28 through D-50 are all present'
else
	fail "decision log is missing:$MISSING_DECISIONS (task 20.15, design §17.1)"
fi

# ── Phase 1 may only be `completed` when the record supports it ─────────────
# This is the clause task 20.15 states outright, and the reason the whole block exists: the
# phase row is the one line a reader trusts, and nothing was stopping it from saying
# `completed` over an incomplete record.
P1_PHASE_STATUS=$(grep -E '^\|[[:space:]]*1[[:space:]]*\|' "$LOWER" |
	awk -F'|' '{ s=$4; gsub(/^[ \t]+|[ \t]+$/, "", s); print s }' | head -n 1)
P1_UNFINISHED=$(grep -E '^\|[[:space:]]*(1?[0-9]|20)\.[0-9]+[[:space:]]*\|' "$LOWER" |
	awk -F'|' '{ s=$5; gsub(/^[ \t]+|[ \t]+$/, "", s);
		if (s == "pending" || s == "in-progress" || s == "blocked") print s }' | wc -l | tr -d ' \t')

# Criteria that are not `done`. The completed clause needs this alongside the leaf count, and it
# was missing: a `pending` criterion is in-vocabulary, so the criteria loop above raises no
# violation for it, and the phase row could therefore read `completed` while a criterion was
# openly unmet. That is exactly what happened -- criterion 14 sat `pending` for the L2 tier being
# implemented but not wired, and nothing in this file objected to `completed` beside it. A phase is
# its criteria; the leaves are only how they were built.
P1_CRIT_UNMET=$(printf '%s\n' "$P1_CRIT" |
	awk -F'|' 'NF >= 5 { s=$4; gsub(/^[ \t]+|[ \t]+$/, "", s); s=tolower(s)
		if (s != "" && s != "done") print s }' | wc -l | tr -d ' \t')

# Deliverable rows that are not `done`. The same hole as the criteria one above, one column over,
# and it was still open after that one was closed: the loop above checks only that a row EXISTS
# for each of 1.1 through 1.11, never what the row SAYS. So deliverables 1.7 and 1.10 sat
# `pending` beside a `completed` phase row, in the same file, nine sections apart, and this script
# passed both. A deliverable is what the phase promised to ship, so a phase cannot be complete
# while one of them is not -- the leaves are how it was built and the criteria are how it is
# judged, but the deliverables are the thing itself.
#
# The row filter is on field 2 rather than NF alone, because `$P1_DELIV` is every line of the
# section: the prose paragraphs, the header row and the `| :--- |` separator all reach this awk,
# and the separator would otherwise count as a row whose status is not `done`.
P1_DELIV_UNMET=$(printf '%s\n' "$P1_DELIV" |
	awk -F'|' 'NF >= 6 && $2 ~ /^[ \t]*\*?\*?1\.[0-9]+\*?\*?[ \t]*$/ {
		s=$5; gsub(/^[ \t]+|[ \t]+$/, "", s); s=tolower(s)
		if (s != "" && s != "done") print s }' | wc -l | tr -d ' \t')

if [ "$P1_PHASE_STATUS" = 'completed' ]; then
	CLAIM_VIOLATIONS=$(wc -l <"$FAILFILE" | tr -d ' \t')
	if [ "${CLAIM_VIOLATIONS:-0}" -ne 0 ]; then
		fail 'Phase 1 is marked `completed` while this check reports violations above (task 20.15)'
	elif [ "${P1_UNFINISHED:-0}" -ne 0 ]; then
		fail "Phase 1 is marked \`completed\` while $P1_UNFINISHED leaf row(s) are not done"
	elif [ "${P1_CRIT_UNMET:-0}" -ne 0 ]; then
		fail "Phase 1 is marked \`completed\` while $P1_CRIT_UNMET completion criterion/criteria are not \`done\`"
	elif [ "${P1_DELIV_UNMET:-0}" -ne 0 ]; then
		fail "Phase 1 is marked \`completed\` while $P1_DELIV_UNMET deliverable row(s) are not \`done\`"
	else
		# The mutation manifest must be checked, and being UNABLE to check it is a failure rather
		# than a pass. The first version of this clause was guarded by
		# `command -v python3 >/dev/null 2>&1 &&`, so on a host where the interpreter is named
		# `python` -- Git Bash on Windows, for one -- the guard short-circuited, the manifest was
		# never read, and the phase was certified `completed` on the strength of a check that had
		# not run. That is the same defect this whole file exists to correct, reproduced inside the
		# clause that certifies the claim, so it now searches for an interpreter and fails if it
		# cannot find one.
		PY=''
		for candidate in python3 python py; do
			if command -v "$candidate" >/dev/null 2>&1; then
				PY="$candidate"
				break
			fi
		done
		if [ -z "$PY" ]; then
			fail 'Phase 1 is marked `completed` but no Python interpreter was found to run scripts/check-mutation-manifest.py; the claim cannot be verified'
		elif ! "$PY" scripts/check-mutation-manifest.py >/dev/null 2>&1; then
			fail 'Phase 1 is marked `completed` but scripts/check-mutation-manifest.py fails: not every Appendix B property has a verified negative control'
		else
			ok "Phase 1 is \`completed\`, and the record supports the claim (manifest verified via $PY)"
		fi
	fi
else
	ok "Phase 1 is \`$P1_PHASE_STATUS\`, with $P1_UNFINISHED leaf row(s), $P1_CRIT_UNMET criterion/criteria and $P1_DELIV_UNMET deliverable row(s) not yet done"
fi

VIOLATIONS=$(wc -l <"$FAILFILE" | tr -d ' \t')
if [ "${VIOLATIONS:-0}" -ne 0 ]; then
	printf '\nPROGRESS.md structure check failed with %s violation(s)\n' "$VIOLATIONS" >&2
	exit 1
fi

printf '\nPROGRESS.md structure check passed\n'
exit 0
