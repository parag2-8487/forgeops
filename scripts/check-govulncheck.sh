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
# All four below are in github.com/docker/docker, all report "Fixed in: N/A"
# (no released Docker version fixes them), and all share one decisive property:
# every example trace govulncheck prints is a package-INIT chain —
#   internal/docker/probe.go:8: docker.init calls client.init, which calls api.init
# — not a call into the affected function. ForgeOps' entire Docker surface is
# internal/docker/probe.go, which calls ONLY Ping() and ServerVersion(); both are
# read-only. Importing the client package is enough to run the vulnerable module's
# init, which is why these appear at all.
#
#   GO-2026-5668  docker cp race: arbitrary empty file creation via symlink swap
#   GO-2026-5617  docker cp race: bind-mount redirection to a host path
#                 → ForgeOps never invokes container copy or archive APIs.
#   GO-2026-4887  AuthZ plugin bypass via oversized request bodies
#   GO-2026-4883  off-by-one in plugin privilege validation
#                 → ForgeOps never installs, configures or calls Docker plugins,
#                   and runs no AuthZ plugin.
#
# Re-review when EITHER:
#   * a fixed docker/docker is released — drop the corresponding entry; or
#   * ForgeOps code starts using container copy/archive APIs or Docker plugins —
#     then these become real findings and must be fixed, not accepted.
#
# Phase 1 note: the agent gains real container operations, so this allowlist must
# be revisited at that point rather than inherited unchanged.
ALLOWLIST=(
  "GO-2026-5668"
  "GO-2026-5617"
  "GO-2026-4887"
  "GO-2026-4883"
)

if ! command -v govulncheck >/dev/null 2>&1; then
  printf 'govulncheck: SKIP not on PATH — install with:\n'
  printf '  go install golang.org/x/vuln/cmd/govulncheck@latest\n'
  exit 0
fi

cd "$(dirname "$0")/../agent"

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT INT TERM

# govulncheck exits 3 when it finds called vulnerabilities; capture rather than abort.
set +e
govulncheck ./... > "$OUT" 2>&1
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
