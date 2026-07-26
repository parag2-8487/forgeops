#!/bin/sh
# Script-level test for scripts/init-env.sh (design §2.2, §13.1, §13.3;
# completion criterion 4; property P-15).
#
# Each case runs the real script inside a throwaway sandbox that mirrors the
# repository layout (`<sandbox>/.env.example` + `<sandbox>/scripts/init-env.sh`),
# so the repository's own `.env` is never touched.
#
# Cases: absent `.env`, repeated runs, pre-existing `.env`, concurrent creators,
# plus a shape check of the committed baseline.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SCRIPT="$ROOT/scripts/init-env.sh"
BASELINE="$ROOT/.env.example"

FAILURES=0

WORKDIR=$(mktemp -d 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-init-env-$$")
mkdir -p "$WORKDIR" || exit 1
trap 'rm -rf "$WORKDIR"' EXIT HUP INT TERM

pass() { printf 'ok   - %s\n' "$1"; }
fail() {
	printf 'FAIL - %s\n' "$1" >&2
	FAILURES=$((FAILURES + 1))
}

# sandbox: prints a fresh, uniquely named sandbox root containing the baseline
# and the script, isolated from every other case. Each call gets its own
# directory, so cases cannot observe one another's `.env`.
sandbox() {
	box=$(mktemp -d "$WORKDIR/case-XXXXXX" 2>/dev/null) || return 1
	mkdir -p "$box/scripts" || return 1
	cp "$BASELINE" "$box/.env.example" || return 1
	cp "$SCRIPT" "$box/scripts/init-env.sh" || return 1
	printf '%s' "$box"
}

run_init() {
	# run_init <sandbox> ; prints nothing, returns the script exit status
	sh "$1/scripts/init-env.sh" >"$1/stdout.log" 2>"$1/stderr.log"
}

echo '# case 1: .env absent -> created from the committed baseline'
box=$(sandbox) || exit 1
if run_init "$box"; then
	if [ -f "$box/.env" ]; then
		if cmp -s "$box/.env" "$box/.env.example"; then
			pass 'absent .env is created byte-identical to .env.example'
		else
			fail 'created .env does not match .env.example'
		fi
	else
		fail 'script succeeded but .env was not created'
	fi
else
	fail "script exited non-zero on a fresh sandbox: $(cat "$box/stderr.log")"
fi

echo '# case 2: repeated runs are idempotent'
box=$(sandbox) || exit 1
run_init "$box" || fail 'first run failed'
cp "$box/.env" "$box/first-run-copy" 2>/dev/null || fail 'first run produced no .env'
if run_init "$box"; then
	if cmp -s "$box/.env" "$box/first-run-copy"; then
		pass 'second run leaves .env byte-identical'
	else
		fail 'second run modified .env'
	fi
else
	fail "second run exited non-zero: $(cat "$box/stderr.log")"
fi
if run_init "$box" && cmp -s "$box/.env" "$box/first-run-copy"; then
	pass 'third run still leaves .env byte-identical'
else
	fail 'third run changed .env or exited non-zero'
fi

echo '# case 3: pre-existing .env is preserved exactly'
box=$(sandbox) || exit 1
printf 'APP_ENV=production\nLOCAL_ONLY=keep-me\n' >"$box/.env"
cp "$box/.env" "$box/expected"
if run_init "$box"; then
	if cmp -s "$box/.env" "$box/expected"; then
		pass 'pre-existing .env is left byte-identical'
	else
		fail 'pre-existing .env was overwritten or merged'
	fi
else
	fail "script exited non-zero with a pre-existing .env: $(cat "$box/stderr.log")"
fi

echo '# case 4: concurrent creators all succeed and produce one complete .env'
box=$(sandbox) || exit 1
WORKERS=8
i=1
while [ "$i" -le "$WORKERS" ]; do
	(
		sh "$box/scripts/init-env.sh" >"$box/out.$i" 2>&1
		printf '%s\n' "$?" >"$box/rc.$i"
	) &
	i=$((i + 1))
done
wait
bad_rc=0
i=1
while [ "$i" -le "$WORKERS" ]; do
	rc=$(cat "$box/rc.$i" 2>/dev/null || printf 'missing')
	[ "$rc" = "0" ] || bad_rc=$((bad_rc + 1))
	i=$((i + 1))
done
if [ "$bad_rc" -eq 0 ]; then
	pass "all $WORKERS concurrent runs exited 0 (a concurrent creator is success)"
else
	fail "$bad_rc of $WORKERS concurrent runs exited non-zero"
fi
if cmp -s "$box/.env" "$box/.env.example"; then
	pass 'concurrent runs leave exactly one complete .env'
else
	fail 'concurrent runs produced a truncated, duplicated or missing .env'
fi

echo '# case 5: missing baseline is a real failure'
box=$(sandbox) || exit 1
rm -f "$box/.env.example"
if run_init "$box"; then
	fail 'script succeeded without a committed .env.example baseline'
else
	pass 'missing .env.example exits non-zero'
fi

echo '# case 6: committed baseline shape (design §13.1)'
malformed=$(grep -vE '^[[:space:]]*(#|$)' "$BASELINE" | grep -vE '^[A-Z][A-Z0-9_]*=' || true)
if [ -z "$malformed" ]; then
	pass 'every non-comment baseline line is KEY=value'
else
	fail "malformed baseline line(s): $malformed"
fi
duplicates=$(grep -E '^[A-Z][A-Z0-9_]*=' "$BASELINE" | sed 's/=.*//' | sort | uniq -d)
if [ -z "$duplicates" ]; then
	pass 'baseline declares every variable exactly once'
else
	fail "duplicated baseline key(s): $duplicates"
fi
if [ -f "$ROOT/.env" ] && cmp -s "$ROOT/.env" "$ROOT/.env.example"; then
	pass 'repository .env (if present) is consistent with the baseline'
fi

printf '\n'
if [ "$FAILURES" -ne 0 ]; then
	printf 'init-env script test FAILED (%s failing assertion(s))\n' "$FAILURES" >&2
	exit 1
fi
echo 'init-env script test passed'
exit 0
