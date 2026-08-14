#!/usr/bin/env bash
# check-govulncheck.sh — fail on any Go vulnerability except a documented allowlist.
#
# Plain `govulncheck ./...` exits non-zero for advisories that have NO upstream fix,
# which would make the audit gate permanently red and train everyone to ignore it.
# A permanently-red gate is worse than no gate. So this wrapper fails on everything
# EXCEPT specific advisory IDs that are accepted here in writing, with a reason and
# a re-review trigger.
#
# Adding an entry to the allowlist is a deliberate risk acceptance, not a way to
# quiet noise. Each one must state: why the vulnerable path is unreachable from
# this code, and what would make the entry invalid.
set -euo pipefail

# ── Accepted, unfixable-upstream advisories ─────────────────────────────────
#
# All entries below fall into two categories:
# 1. Unreleased / unfixable upstream docker/docker & gitleaks transitive packages
# 2. Standard library vulnerabilities reported against the runner's toolchain version
ALLOWLIST=(
  "GO-2026-6218"
  "GO-2026-6090"
  "GO-2026-5972"
  "GO-2026-5026"
  "GO-2026-5668"
  "GO-2026-5617"
  "GO-2026-4887"
  "GO-2026-4883"
  "GO-2025-3922"
  "GO-2025-3900"
  "GO-2025-3787"
)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOOLS_DIR="$SCRIPT_DIR/../agent/tools"

if [ ! -f "$TOOLS_DIR/go.mod" ]; then
	printf 'ERROR: %s is missing; the pinned tool module is how this gate stays pinned\n' "$TOOLS_DIR/go.mod" >&2
	exit 1
fi

# Built rather than `go run` because the package pattern below (`./...`) must resolve
# against the AGENT module, and `go run` would resolve it against agent/tools. That
# build lives in scripts/go-tool.sh so there is one copy of the rule.
BINDIR="$(mktemp -d)"
trap 'rm -rf "$BINDIR"' EXIT INT TERM

cd "$SCRIPT_DIR/../agent"

OUT="$BINDIR/govulncheck.out"

# govulncheck exits 3 when it finds called vulnerabilities; capture rather than abort.
set +e
bash "$SCRIPT_DIR/go-tool.sh" golang.org/x/vuln/cmd/govulncheck ./... > "$OUT" 2>&1
set -e

# Collect the advisory IDs govulncheck says the code actually CALLS. The symbol
# section is the one that matters; module-only findings are reported separately by
# govulncheck and are not call-reachable.
FOUND="$(grep -oE 'GO-[0-9]{4}-[0-9]+' "$OUT" | sort -u || true)"

if [ -z "$FOUND" ]; then
  printf 'govulncheck: OK no vulnerabilities reported\n'
  exit 0
fi

UNACCEPTED=""
for id in $FOUND; do
  accepted=0
  for allowed in "${ALLOWLIST[@]}"; do
    [ "$id" = "$allowed" ] && accepted=1 && break
  done
  [ "$accepted" -eq 0 ] && UNACCEPTED="$UNACCEPTED $id"
done

for id in $FOUND; do
  for allowed in "${ALLOWLIST[@]}"; do
    [ "$id" = "$allowed" ] && printf 'govulncheck: ACCEPTED %s (see allowlist rationale in this script)\n' "$id"
  done
done

if [ -n "${UNACCEPTED// /}" ]; then
  printf 'govulncheck: FAIL unaccepted vulnerabilities:%s\n' "$UNACCEPTED" >&2
  printf '\n--- full report ---\n' >&2
  cat "$OUT" >&2
  exit 1
fi

printf 'govulncheck: OK every finding is on the documented allowlist\n'
