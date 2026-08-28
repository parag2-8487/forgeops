#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# What this gates: that the three records of what is built agree with each other.
#
# Three files claim to say which leaves are done, and nothing compared them:
#
#   * PROGRESS.md            — a status table, one row per leaf, with a status column.
#   * tasks.md               — the plan, one checkbox per leaf.
#   * PROGRESS.md, again     — a second "leaf evidence" table naming what was run.
#
# A leaf marked `done` in the first with no checkbox in the second, or a checkbox with no
# evidence row, is a claim with nothing behind it. That is the failure mode this repository
# keeps rediscovering: a record that reads as coverage while the thing it records has drifted.
# Every disagreement is an error and fails the run.
#
# Promoted from the untracked `scripts/_state.sh`, which did this reconciliation by hand for
# months and ran in no workflow, so nothing noticed when a row and its checkbox diverged. The
# logic is unchanged; the exit code is new, and so is being wired into CI's `audit` job.
#
# Two filters, both earned rather than cosmetic:
#   * Phase 0 ids (`0.x`) are skipped. They are `done` in PROGRESS.md and are not in the
#     Phase 1 plan at all, so without the filter forty of them show up as disagreements
#     every run and a real one is invisible in the noise.
#   * Rows whose status column is empty are skipped. The "Phase 1 leaf evidence" table
#     shares the `| <id> |` shape with the status table but has three columns, so its rows
#     were being counted as a status of "".
set -euo pipefail
cd "$(dirname "$0")/.."

PLAN=.antigravity/specs/phase-1-mvp-core/tasks.md
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
status=0

# A markdown table cell may contain a literal pipe written `\|`. That renders correctly and is the
# right way to write it, but a naive field split on `|` still breaks the row in two -- which is what
# happened to leaf 8.5, whose evidence prose quotes a shell pipeline. Normalising the escape to a
# placeholder first makes every column index below mean what it says. Doing it once, here, rather
# than in each awk program, is what stops the three counts from disagreeing with each other.
RECORD="$work/progress.md"
sed 's/\\|/\xC2\xA6/g' PROGRESS.md >"$RECORD"

echo "=== PROGRESS.md Phase 1 leaf status counts ==="
# The vocabulary is closed: `pending`, `in-progress`, `done`, `blocked`. Anything else means the row
# is malformed -- and one was, because its evidence prose contained a `|`, which shifted every cell
# after it and printed a paragraph where the status belongs. Printing that as though it were a
# status is how a reader learns to skim this output, so an unrecognised value is an error.
awk -F'|' '
  /^\| [0-9]+\.[0-9]+ +\|/ {
    id=$2; s=$5; gsub(/ /,"",id); gsub(/ /,"",s)
    if (id ~ /^0\./ || s == "") next
    if (s != "pending" && s != "in-progress" && s != "done" && s != "blocked") {
      printf "MALFORMED %s: status column reads %.60s...\n", id, s
      bad++
      next
    }
    c[s]++; t++
  }
  END {
    for (k in c) printf "%-12s %d\n", k, c[k]
    printf "%-12s %d\n", "TOTAL", t
    if (bad) {
      printf "\nFAIL: %d row(s) have an unrecognised status. A `|` inside evidence prose shifts\n", bad
      printf "      every cell after it; escape it as \\| so the row still parses.\n"
      exit 1
    }
  }
' "$RECORD" || status=1

echo
echo "=== tasks.md leaf checkbox counts ==="
echo "checked   $(grep -cE '^  - \[x\] [0-9]+\.[0-9]+' "$PLAN" || true)"
echo "unchecked $(grep -cE '^  - \[ \] [0-9]+\.[0-9]+' "$PLAN" || true)"

# `comm` requires LEXICALLY sorted input and warns "file 1 is not in sorted order" on
# anything else, then exits non-zero -- which is what `sort -V` produced here, because
# version order puts 9.10 before 9.2 and lexical order does not. The comparisons below are
# set operations, so the order only has to agree between the two sides; `sort` plain is the
# order `comm` documents.
awk -F'|' '
  /^\| [0-9]+\.[0-9]+ +\|/ {
    id=$2; s=$5; gsub(/ /,"",id); gsub(/ /,"",s)
    if (id ~ /^0\./ || s != "done") next
    print id
  }
' "$RECORD" | sort >"$work/done.txt"
grep -oE '^  - \[x\] [0-9]+\.[0-9]+' "$PLAN" | awk '{print $NF}' | sort >"$work/checked.txt"

# Evidence, and why this is a range assertion rather than a per-leaf one.
#
# The "Phase 1 leaf evidence" table is introduced by the sentence "Every `done` row above is
# answered here". That was true when it was written and stopped being true at leaf 9.2: the
# table has 68 rows and there are 205 done rows. Wiring this script up is what surfaced it.
#
# The honest fix is not to invent 96 evidence rows -- fabricated evidence is worse than absent
# evidence, and this repository has a history of exactly that. From 9.3 onward evidence lives in
# the "Completion criteria" and "Deliverable coverage" sections, per bullet, and those sections
# say what was run. So this gate asserts the two things that can be checked exactly:
#
#   * the two directions of plan-versus-record agreement, per leaf, with no tolerance; and
#   * that the evidence table's declared range matches the rows it actually has, so the
#     "answered here" claim cannot drift again without failing.
# Bounded to the section rather than to "any leaf-shaped row with no status cell". Three sibling
# tables -- the wording-change notes and the findings tables -- share the `| <id> |` shape, so the
# unbounded count read 72 where the table holds 68, and a check that miscounts by four is a check a
# reader learns to ignore.
awk '
  /^## Phase 1 leaf evidence/ { inside = 1; next }
  /^## / { inside = 0 }
  inside && /^\| [0-9]+\.[0-9]+ +\|/ {
    split($0, cell, "|"); id = cell[2]; gsub(/ /, "", id); print id
  }
' "$RECORD" | sort -V >"$work/ev_table.txt"
ev_count=$(grep -c . "$work/ev_table.txt" || true)
ev_last=$(tail -n 1 "$work/ev_table.txt")
declared=$(grep -oE 'covers leaves 1\.1 to [0-9]+\.[0-9]+ \([0-9]+ rows\)' "$RECORD" | head -n 1 || true)
echo
echo "=== leaf evidence table ==="
echo "actual:   covers leaves 1.1 to $ev_last ($ev_count rows)"
echo "declared: ${declared:-<no range declared in PROGRESS.md>}"
if [ "$declared" != "covers leaves 1.1 to $ev_last ($ev_count rows)" ]; then
	echo
	echo "FAIL: PROGRESS.md's description of the leaf evidence table does not match the table."
	echo "      It must contain the exact phrase: covers leaves 1.1 to $ev_last ($ev_count rows)"
	status=1
fi

report() {
	local label=$1 file=$2
	if [ -s "$file" ]; then
		echo
		echo "FAIL: $label"
		sed 's/^/  /' "$file"
		status=1
	fi
}

comm -23 "$work/done.txt" "$work/checked.txt" >"$work/d1.txt"
comm -13 "$work/done.txt" "$work/checked.txt" >"$work/d2.txt"

report "PROGRESS.md says done but tasks.md is unchecked" "$work/d1.txt"
report "tasks.md is checked but PROGRESS.md is not done" "$work/d2.txt"

echo
if [ "$status" -eq 0 ]; then
	echo "ok:   the plan and the record agree, and the evidence table describes itself correctly"
else
	echo "The records disagree. A leaf that is done must be checked in the plan, and the evidence"
	echo "table's stated range must match the rows it actually holds."
fi
exit "$status"
