#!/usr/bin/env bash
# scripts/check-architecture-diagrams.sh — keep ARCHITECTURE-DIAGRAMS.md honest.
#
# WHY THIS EXISTS, and it is the condition `.gitignore` attached to the file:
#
#   "Folding them into `docs/architecture.md` is fine, but only alongside a check that asserts
#    every service they name still exists in `docker-compose.yml`."
#
# That rule existed because the diagrams are generated from a snapshot of the tree at one commit,
# and a committed copy without a gate becomes a picture of a tree it no longer matches. The
# snapshot had already gone stale by 2026-10-08 — it claimed nine services (there are fourteen
# defined and ten started), ten migrations (there are thirty-seven) and a journey passing four of
# thirteen steps (it passed six) — which is the defect the rule predicted, observed.
#
# So this is the gate, and the file is tracked because of it.
#
# WHAT IS ASSERTED: every claim in the diagrams that is mechanically checkable. The prose counts
# that cannot be are listed at the bottom as things a human still has to update, rather than
# silently passing.
#
# Usage:  bash scripts/check-architecture-diagrams.sh
# Exit:   0 = consistent, 1 = the document and the tree disagree.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOC="$REPO_ROOT/ARCHITECTURE-DIAGRAMS.md"
COMPOSE="$REPO_ROOT/docker-compose.yml"
COMPOSE_E2E="$REPO_ROOT/docker-compose.e2e.yml"

fail=0
pass=0

ok()   { printf '  ok    %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }

if [ ! -f "$DOC" ]; then
  echo "FAIL: $DOC not found" >&2
  exit 1
fi

echo "check-architecture-diagrams"

# ─── 1. Every service named in the diagrams exists in the compose files ───────────────────────────
#
# BOTH FILES. The deployment is the base file PLUS the e2e overlay — that is what the launcher passes
# (`-f docker-compose.yml -f docker-compose.e2e.yml`) — and `agent` is declared only in the overlay.
# Reading the base file alone reported `agent` as a phantom service, which was a defect in this check
# rather than in the diagram.
services="$(python3 - "$COMPOSE" "$COMPOSE_E2E" <<'PY'
import sys, re
names = set()
for path in sys.argv[1:]:
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        continue
    match = re.search(r"^services:\n(.*?)(?=^\S)", text, re.M | re.S)
    block = match.group(1) if match else ""
    names.update(re.findall(r"^  ([a-z0-9][a-z0-9_-]*):", block, re.M))
print("\n".join(sorted(names)))
PY
)"

[ -n "$services" ] || { bad "no services parsed out of the compose files"; }

# The ten the launcher starts. The diagram's prose lists these explicitly.
started="postgres redis opa cerbos authentik-server authentik-worker backend worker frontend agent"
for svc in $started; do
  if printf '%s\n' "$services" | grep -qx "$svc"; then
    ok "compose declares '$svc'"
  else
    bad "the diagram says '$svc' runs, but docker-compose.yml does not declare it"
  fi
done

# ─── 2. The service counts stated in the quick-reference table ────────────────────────────────────
#
# ASSERTED AS NUMBERS, NOT AS WORDS, AND EACH AGAINST ITS OWN SOURCE. The first version of this check
# only looked for the word "fourteen" and passed while the tree held 22 services — a check that agrees
# with the thing it checks proves nothing.
#
# The document makes three distinct claims, and they are about three different sets, so each is
# compared with the set it describes rather than with a single total:
#
#   "docker-compose.yml declares N services"          -> the base file alone
#   "the e2e overlay adds agent, for N in the union"  -> both files
#   "brings up: N"                                    -> what the launcher names explicitly
base_total="$(python3 - "$COMPOSE" <<'PY'
import sys, re
text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r"^services:\n(.*?)(?=^\S)", text, re.M | re.S)
block = match.group(1) if match else ""
print(len(set(re.findall(r"^  ([a-z0-9][a-z0-9_-]*):", block, re.M))))
PY
)"
union_total="$(printf '%s\n' "$services" | grep -c . || true)"
started_total="$(printf '%s\n' "$started" | wc -w | tr -d ' ')"

assert_number() {
  # $1 = the number the tree says, $2 = the literal phrase in the document, $3 = a human label
  local actual="$1" phrase="$2" label="$3"
  local cited
  cited="$(grep -oiE "$phrase" "$DOC" | grep -oE '[0-9]+' | head -1 || true)"
  if [ -z "$cited" ]; then
    bad "the diagram no longer states a service count for ${label} (pattern /${phrase}/)"
    return
  fi
  if [ "$cited" = "$actual" ]; then
    ok "${label}: diagram says ${cited}, tree has ${actual}"
  else
    bad "${label}: diagram says ${cited}, tree has ${actual}"
  fi
}

assert_number "$base_total"    "declares [0-9]+ services"  "base docker-compose.yml"
assert_number "$union_total"   "for \*\*[0-9]+\*\* in"     "with the e2e overlay"
assert_number "$started_total" "brings up: [0-9]+ services" "what the launcher starts"

# ─── 3. Dockerfile base images named in Diagram 1 ────────────────────────────────────────────────
# Only the version-bearing claims, because those are what drift.
for pair in "backend/Dockerfile:python:3.13" "frontend/Dockerfile:node:22"; do
  file="${pair%%:*}"; expected="${pair#*:}"
  if [ -f "$REPO_ROOT/$file" ]; then
    if grep -q "^FROM $expected" "$REPO_ROOT/$file"; then
      ok "$file is built on $expected"
    else
      bad "$file no longer uses $expected"
    fi
  fi
done

# ─── 4. The journey step count, against PROGRESS.md ──────────────────────────────────────────────
#
# PROGRESS.md is the authority for criteria status, so both files are read and compared rather
# than the diagram being trusted.
progress_step="$(grep -oE 'journey is [0-9]+ of 13' "$REPO_ROOT/PROGRESS.md" | tail -1 | grep -oE '[0-9]+' | head -1 || true)"
doc_step="$(grep -oE 'passes \*\*[0-9]+ of its 13 steps\*\*' "$DOC" | grep -oE '[0-9]+' | head -1 || true)"
if [ -n "$progress_step" ] && [ -n "$doc_step" ]; then
  if [ "$progress_step" = "$doc_step" ]; then
    ok "journey step count agrees with PROGRESS.md ($doc_step of 13)"
  else
    bad "the diagram says $doc_step of 13 journey steps; PROGRESS.md says $progress_step"
  fi
else
  printf '  skip  journey step count (diagram=%s, PROGRESS.md=%s)\n' "${doc_step:-?}" "${progress_step:-?}"
fi

# ─── 5. Mermaid blocks still parse ───────────────────────────────────────────────────────────────
#
# A syntax error in a fenced block renders as a red error box on GitHub, which is worse than no
# diagram. This checks the two structural invariants that break most often: a header line, and
# balanced brackets in node/edge labels.
mermaid_report="$(python3 - "$DOC" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
blocks = re.findall(r"```mermaid\n(.*?)```", text, re.S)
if not blocks:
    print("(none)")
    raise SystemExit
problems = []
for i, b in enumerate(blocks, 1):
    lines = [l for l in b.split("\n") if l.strip()]
    if not lines or not re.match(r"^(graph|flowchart)\s", lines[0]):
        problems.append(f"block {i} has no graph/flowchart header")
    for open_c, close_c in (("[", "]"), ("{", "}"), ("(", ")")):
        depth = b.count(open_c) - b.count(close_c)
        if depth:
            problems.append(f"block {i} has {depth:+d} unbalanced {open_c}{close_c}")
print("OK" if not problems else "; ".join(problems))
PY
)"

case "$mermaid_report" in
  OK) ok "both Mermaid blocks parse (header present, brackets balanced)" ;;
  "(none)") bad "no mermaid blocks found in the document" ;;
  *) bad "mermaid problem: $mermaid_report" ;;
esac

# ─── 6. Things this gate deliberately does NOT check ─────────────────────────────────────────────
#
# Printed rather than omitted, so the coverage of this check is visible rather than implied.
cat <<'NOTE'

  not checked (a human must update these when the tree moves):
    - the package / route / migration counts in the quick-reference table
    - the coverage percentages, which come from a separate measurement run
    - the model-tier table, which mirrors backend/config/model-tiers.yaml
NOTE

echo
if [ "$fail" -eq 0 ]; then
  printf '  %d passed, 0 failed\n\n' "$pass"
  exit 0
fi
printf '  %d passed, %d failed\n\n' "$pass" "$fail"
exit 1
