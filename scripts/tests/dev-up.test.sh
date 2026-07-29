#!/bin/sh
# Script-level test for scripts/dev-up.sh and the `up` target wiring
# (design.md §2.2, §13.3–§13.4; completion criteria 4 and 5).
#
# Scope split, deliberately:
#   * The readiness POLLING behaviour — success, timeout and the named
#     per-dependency failure output — is exercised against a live stand-in server
#     by scripts/tests/dev_up_readiness_test.py. That harness runs the server on a
#     thread inside its own process and bounds every dev-up.sh invocation with a
#     subprocess timeout. Managing a background server from a POSIX script under
#     MSYS orphans processes that keep the caller's pipes open and hang it, so
#     that responsibility is deliberately NOT in this file.
#   * This file covers what can be asserted without any server: the profile
#     discipline, the Compose gate, the init-env prerequisite, and the promise
#     that `up` never disturbs an existing .env.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT" || exit 1

SCRIPT=scripts/dev-up.sh
READINESS_HARNESS=scripts/tests/dev_up_readiness_test.py
FAILURES=0

pass() { printf 'ok   - %s\n' "$1"; }
fail() {
	printf 'FAIL - %s\n' "$1" >&2
	FAILURES=$((FAILURES + 1))
}

WORK=$(mktemp -d 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-devup-$$")
mkdir -p "$WORK" || exit 1
trap 'rm -rf "$WORK"' EXIT HUP INT TERM

if [ ! -f "$SCRIPT" ]; then
	printf 'FAIL - %s is missing\n' "$SCRIPT" >&2
	exit 1
fi

# Executable code only: dev-up.sh documents WHY it passes no --profile flag, and
# matching that prose would be a false positive.
grep -v '^[[:space:]]*#' "$SCRIPT" | sed 's/[[:space:]]#.*$//' >"$WORK/dev-up.code"

echo '# case 1: dev-up starts only the unprofiled default set'
if grep -F -- '--profile' "$WORK/dev-up.code" >/dev/null 2>&1; then
	fail 'dev-up.sh must never pass --profile; optional services are separate commands'
else
	pass 'dev-up.sh executable code passes no --profile flag'
fi
if grep -F 'up -d --wait' "$WORK/dev-up.code" >/dev/null 2>&1; then
	pass 'uses the unprofiled `docker compose up -d --wait` gate'
else
	fail 'dev-up.sh does not use `docker compose up -d --wait`'
fi

echo '# case 2: dev-up gates on READINESS, not on the container liveness check'
if grep -F '/health/ready' "$WORK/dev-up.code" >/dev/null 2>&1; then
	pass 'polls /health/ready'
else
	fail 'dev-up.sh must poll /health/ready (design §4.4, §13.3)'
fi
if grep -E 'FORGEOPS_READY_TIMEOUT|READY_TIMEOUT' "$WORK/dev-up.code" >/dev/null 2>&1; then
	pass 'the readiness wait is bounded by a timeout'
else
	fail 'the readiness poll must be bounded'
fi

echo '# case 3: the readiness behaviour harness exists and is wired'
if [ -f "$READINESS_HARNESS" ]; then
	pass "readiness behaviour harness present: $READINESS_HARNESS"
else
	fail "missing readiness behaviour harness: $READINESS_HARNESS"
fi

echo '# case 4: `make up` declares init-env as a prerequisite'
if grep -E '^up:[[:space:]]+init-env' Makefile >/dev/null 2>&1; then
	pass 'Makefile declares `up: init-env`'
else
	fail 'Makefile must declare init-env as an explicit prerequisite of up (design §13.3)'
fi
for target in down logs; do
	if grep -E "^$target:" Makefile >/dev/null 2>&1; then
		pass "Makefile declares the $target target"
	else
		fail "Makefile is missing the $target target (design §13.4)"
	fi
done
# `down` must not destroy volumes.
DOWN_RECIPE=$(awk '/^down:/{f=1;next} /^[a-zA-Z0-9_.-]+:/{f=0} f' Makefile)
case $DOWN_RECIPE in
*--volumes* | *' -v'*)
	fail 'make down must not remove Docker volumes (design §13.4)'
	;;
*)
	pass 'make down preserves Docker volumes'
	;;
esac

echo '# case 5: a pre-existing .env is preserved byte-for-byte by the up prerequisite'
BOX="$WORK/envbox"
mkdir -p "$BOX/scripts"
cp .env.example "$BOX/.env.example"
cp scripts/init-env.sh "$BOX/scripts/init-env.sh"
printf 'APP_ENV=production\nLOCAL_ONLY=keep-me\n' >"$BOX/.env"
cp "$BOX/.env" "$BOX/.env.expected"
(cd "$BOX" && sh scripts/init-env.sh >/dev/null 2>&1)
if cmp -s "$BOX/.env" "$BOX/.env.expected"; then
	pass 'the init-env prerequisite of `up` leaves an existing .env byte-identical'
else
	fail 'the init-env prerequisite modified an existing .env'
fi

echo '# case 6: a fresh clone with no .env can still start Compose directly'
if grep -F 'required: true' docker-compose.yml >/dev/null 2>&1 &&
	grep -F 'required: false' docker-compose.yml >/dev/null 2>&1; then
	pass '.env.example is a required env file and .env an optional override'
else
	fail 'docker-compose.yml must load .env.example as required and .env as optional (design §13.3)'
fi

printf '\n'
if [ "$FAILURES" -ne 0 ]; then
	printf 'dev-up script test FAILED (%s failing assertion(s))\n' "$FAILURES" >&2
	exit 1
fi
echo 'dev-up script test passed'
exit 0
