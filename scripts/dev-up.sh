#!/bin/sh
# Phase 0 development bring-up (design.md §2.2, §4.4, §13.3, §13.4).
#
# Starts EXACTLY the unprofiled default Compose profile and then waits for the
# backend to become READY:
#
#   1. `docker compose up -d --wait` — Compose's own gate gets every default
#      service to its container healthcheck. For the backend that healthcheck is
#      deliberately LIVENESS only (§4.4), because a container must not be
#      declared unhealthy just because PostgreSQL is briefly unavailable.
#   2. `GET /health/ready` polling — readiness is the gate a developer actually
#      cares about, and it is asserted here rather than in Compose so that the
#      two concerns stay separate. On timeout the failing dependencies are named
#      from the RFC 9457 problem body instead of printing a bare timeout.
#
# No profile is ever passed, so `vault` and `tools` services are never started.
#
# Overrides (used by scripts/tests/dev-up.test.sh so the polling, timeout and
# failure-reporting paths can be exercised without a container engine):
#   FORGEOPS_READY_URL       readiness URL to poll   (default http://localhost:${BACKEND_PORT:-8000}/health/ready)
#   FORGEOPS_READY_TIMEOUT   seconds before giving up (default 180)
#   FORGEOPS_READY_INTERVAL  seconds between polls    (default 2)
#   FORGEOPS_SKIP_COMPOSE    when non-empty, skip the `docker compose up` step

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

BACKEND_PORT_DEFAULT=8000
READY_URL=${FORGEOPS_READY_URL:-http://localhost:${BACKEND_PORT:-$BACKEND_PORT_DEFAULT}/health/ready}
READY_TIMEOUT=${FORGEOPS_READY_TIMEOUT:-180}
READY_INTERVAL=${FORGEOPS_READY_INTERVAL:-2}

log() { printf '==> dev-up: %s\n' "$1"; }
err() { printf 'dev-up: %s\n' "$1" >&2; }

# ── 1. Start the default profile ─────────────────────────────────────────────
if [ -n "${FORGEOPS_SKIP_COMPOSE:-}" ]; then
	log "skipping 'docker compose up' (FORGEOPS_SKIP_COMPOSE is set)"
else
	if ! command -v docker >/dev/null 2>&1; then
		err 'docker is not installed or not on PATH; install Docker with the Compose 2.24.7 plugin'
		exit 1
	fi
	log 'starting the default Compose profile (postgres, redis, opa, backend, frontend)'
	# No --profile flag: the unprofiled default set only.
	if ! docker compose up -d --wait; then
		err 'docker compose up -d --wait failed'
		err 'inspect the services with: make logs'
		exit 1
	fi
fi

# ── 2. Poll readiness ────────────────────────────────────────────────────────
# fetch_ready writes the response body to $1 and prints the HTTP status code.
fetch_ready() {
	_body=$1
	if command -v curl >/dev/null 2>&1; then
		curl -sS -o "$_body" -w '%{http_code}' --max-time 5 "$READY_URL" 2>/dev/null ||
			printf '000'
	elif command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; then
		_py=$(command -v python3 || command -v python)
		"$_py" - "$READY_URL" "$_body" <<'PY' 2>/dev/null || printf '000'
import sys, urllib.request, urllib.error
url, out = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(url, timeout=5) as r:
        body, status = r.read(), r.status
except urllib.error.HTTPError as e:
    body, status = e.read(), e.code
except Exception:
    print("000", end="")
    sys.exit(0)
open(out, "wb").write(body)
print(status, end="")
PY
	else
		err 'neither curl nor python is available to poll readiness'
		return 1
	fi
}

BODY=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-ready-$$")
trap 'rm -f "$BODY"' EXIT HUP INT TERM

log "waiting up to ${READY_TIMEOUT}s for readiness at $READY_URL"

ELAPSED=0
STATUS=000
while [ "$ELAPSED" -lt "$READY_TIMEOUT" ]; do
	: >"$BODY"
	STATUS=$(fetch_ready "$BODY")
	if [ "$STATUS" = "200" ]; then
		log 'backend reports ready (PostgreSQL and Redis both answered)'
		exit 0
	fi
	sleep "$READY_INTERVAL"
	ELAPSED=$((ELAPSED + READY_INTERVAL))
done

# ── 3. Named failure output ──────────────────────────────────────────────────
err "backend did not become ready within ${READY_TIMEOUT}s (last HTTP status: ${STATUS})"
if [ -s "$BODY" ]; then
	# The readiness problem document carries one errors[] entry per failed or
	# timed-out dependency; surface those names rather than a bare timeout.
	FAILED=$(tr -d '\r\n' <"$BODY" | tr ',' '\n' | grep -o '"dependency"[[:space:]]*:[[:space:]]*"[^"]*"' |
		sed 's/.*"\([^"]*\)"$/\1/' | sort -u | tr '\n' ' ')
	if [ -n "$FAILED" ]; then
		err "unready dependencies: $FAILED"
	fi
	err 'readiness response body:'
	sed 's/^/    /' <"$BODY" >&2
else
	err 'no readiness response body was received (is the backend running?)'
fi
err 'inspect the services with: make logs'
exit 1
