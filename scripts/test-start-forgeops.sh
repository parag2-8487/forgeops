#!/usr/bin/env bash
#
# Verify scripts/start-forgeops.sh WITHOUT running its mutating steps.
#
# The launcher runs top to bottom, so it cannot be sourced without starting containers. This extracts
# every top-level function definition and exercises them in the CURRENT shell, then checks the files
# and command shapes it depends on. Nothing here writes to .env or touches a container.
#
# Run it on real Linux, which is the point:
#     docker run --rm -v "$PWD:/mnt" -w /mnt ubuntu:24.04 bash scripts/test-start-forgeops.sh
#
# TWO MISTAKES THIS FILE HAS ALREADY MADE, both of which produced FALSE PASSES rather than errors,
# and both worth keeping in mind before trusting any harness:
#
#   1. The extraction stopped at a banner matched with a pattern that counted box-drawing characters
#      as one character each. They are three bytes each in UTF-8, so the pattern never matched, the
#      whole launcher was sourced, and it ran.
#   2. Tests were run through `bash -c`, which does not inherit shell functions. Every function was
#      "command not found" -- and several assertions then compared two empty strings and PASSED.
#      A test that cannot see the code under test must fail loudly, never quietly agree.

# shellcheck disable=SC2034
#
# File-level, and it must appear before the first COMMAND to take effect. This harness EXTRACTS
# functions out of the launcher and sources them, so the variables those functions read -- the colour
# codes, STEP, ENV_PATH, STACK_IS_UP -- are assigned here and consumed in code shellcheck is not
# analysing. Every one of them looks unused and none of them is. A per-line directive does not work
# either: several are assigned on one line separated by `;`, which shellcheck treats as separate
# commands, so it would cover only the first.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$REPO_ROOT/scripts/start-forgeops.sh"

PASS=0
FAIL=0

# Tests run in THIS shell so the extracted functions are visible. Each test is a function that
# returns non-zero and prints why.
check() {
  local name="$1" fn="$2"
  local out rc
  out="$("$fn" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    PASS=$((PASS + 1)); printf '  PASS  %s\n' "$name"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL  %s -> %s\n' "$name" "${out:-exit $rc}"
  fi
}

printf '\nExtracting the launcher functions\n'

# Every top-level function in the launcher is written as `name() {` at column 0. Multi-line bodies
# close with `}` at column 0; SINGLE-LINE definitions such as `ok_() { :; }` close on the same line.
# Handling only the first shape leaves the block open forever and captures the entire rest of the
# file, which then EXECUTES on source -- that is what happened, and it failed on an unbound colour
# variable rather than announcing the real problem.
LIB="$(mktemp)"
awk '
  /^[a-z_][a-z0-9_]*\(\) \{/ {
    print
    if ($0 ~ /\}[[:space:]]*$/) { inside = 0 } else { inside = 1 }
    next
  }
  inside { print }
  inside && /^\}/ { inside = 0 }
' "$TARGET" > "$LIB"

# The colour variables are assigned above the first function and so are not extracted. Defined empty
# before sourcing, because the extracted display helpers reference them and `set -u` is on.
#
# shellcheck disable=SC2034  # consumed by the SOURCED functions, which shellcheck cannot see
C_HEAD=''; C_OK=''; C_WARN=''; C_ERR=''; C_DIM=''; C_RESET=''; C_WHITE=''
# shellcheck disable=SC2034  # step_() increments this
STEP=0

# shellcheck source=/dev/null
source "$LIB"

EXPECTED_FUNCS=(have_ port_free_ usable_port_ resolve_port_ env_get_ env_set_ new_secret_ need_secret_)
missing=""
for f in "${EXPECTED_FUNCS[@]}"; do
  if ! declare -F "$f" >/dev/null 2>&1; then missing="$missing $f"; fi
done
if [ -n "$missing" ]; then
  printf '  FAIL  the extraction did not yield:%s\n' "$missing"
  printf '\n  passed 0, failed 1\n\n'
  exit 1
fi
printf '  extracted %s functions from %s lines\n' "${#EXPECTED_FUNCS[@]}" "$(wc -l < "$LIB")"

# The display helpers live above the first function and are not extracted, so stub them. Kept silent
# so a helper writing to stdout cannot be mistaken for a test result.
ok_()   { :; }
info_() { :; }
warn_() { :; }
die_()  { printf 'die_: %s\n' "$1"; return 1; }

# port_free_ shells out to $LAUNCHER_PY. Point it at whatever python this image has.
LAUNCHER_PY="$(command -v python3 || command -v python || true)"
export LAUNCHER_PY
if [ -z "$LAUNCHER_PY" ]; then
  printf '  FAIL  no python3 in this image, so port probing cannot be tested\n'
  exit 1
fi

printf '\nPort probing\n'

t_free_port_is_free() {
  port_free_ 47821 || { echo "a free port was reported busy"; return 1; }
}

t_busy_port_is_busy() {
  python3 - <<'PY' &
import socket, time
s = socket.socket()
s.bind(("127.0.0.1", 47822))
s.listen(1)
time.sleep(4)
PY
  local pid=$!
  sleep 1
  if port_free_ 47822; then
    kill "$pid" 2>/dev/null
    echo "a bound port was reported free"
    return 1
  fi
  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  return 0
}

t_preferred_port_kept() {
  local got; got="$(usable_port_ 47823 test)"
  [ "$got" = "47823" ] || { echo "got [$got]"; return 1; }
}

t_busy_port_moves_forward() {
  python3 - <<'PY' &
import socket, time
s = socket.socket()
s.bind(("127.0.0.1", 47824))
s.listen(1)
time.sleep(6)
PY
  local pid=$!
  sleep 1
  local got; got="$(usable_port_ 47824 test)"
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  [ -n "$got" ] || { echo "returned nothing"; return 1; }
  [ "$got" != "47824" ] || { echo "returned the busy port"; return 1; }
  [ "$got" -gt 47824 ] || { echo "went backwards to $got"; return 1; }
}

t_running_stack_skips_probing() {
  # With STACK_IS_UP=1 the recorded value must be returned untouched even when the port is busy,
  # because our own containers are what hold it.
  python3 - <<'PY' &
import socket, time
s = socket.socket()
s.bind(("127.0.0.1", 47825))
s.listen(1)
time.sleep(5)
PY
  local pid=$!
  sleep 1
  local t; t="$(mktemp)"; printf 'BACKEND_PORT=47825\n' > "$t"
  local got; got="$(ENV_PATH="$t" STACK_IS_UP=1 resolve_port_ BACKEND_PORT 18000 backend)"
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  [ "$got" = "47825" ] || { echo "moved the port to [$got] while the stack was up"; return 1; }
}

check 'a free port is reported free'                t_free_port_is_free
check 'a bound port is reported busy'               t_busy_port_is_busy
check 'the preferred port is kept when free'        t_preferred_port_kept
check 'a busy port moves forward'                   t_busy_port_moves_forward
check 'a running stack keeps its recorded ports'    t_running_stack_skips_probing

printf '\nSecret generation\n'

t_secret_shape() {
  local v; v="$(new_secret_ 16)"
  case "$v" in
    local-only-not-a-real-secret-*) ;;
    *) echo "unexpected prefix: [$v]"; return 1 ;;
  esac
  local hex="${v#local-only-not-a-real-secret-}"
  [ "${#hex}" -eq 32 ] || { echo "expected 32 hex chars, got ${#hex}"; return 1; }
  case "$hex" in
    *[^0-9a-f]*) echo "not lowercase hex: [$hex]"; return 1 ;;
  esac
}

t_secret_unique() {
  local a b; a="$(new_secret_ 16)"; b="$(new_secret_ 16)"
  [ "$a" != "$b" ] || { echo "two calls produced the same value"; return 1; }
}

check 'a generated secret has the labelled prefix and hex body' t_secret_shape
check 'two generated secrets differ'                            t_secret_unique

printf '\nReading and writing .env\n'

SAMPLE="$(mktemp)"
cat > "$SAMPLE" <<'EOF'
# a leading comment
PLAIN=value
QUOTED="quoted value"
WITH_COMMENT=abc            # trailing note
EMPTY=
NUMBER=15432

#COMMENTED_OUT=nope
EOF

t_get_plain() {
  local v; v="$(ENV_PATH="$SAMPLE" env_get_ PLAIN)"
  [ "$v" = "value" ] || { echo "got [$v]"; return 1; }
}

t_get_strips_comment() {
  local v; v="$(ENV_PATH="$SAMPLE" env_get_ WITH_COMMENT)"
  [ "$v" = "abc" ] || { echo "got [$v]"; return 1; }
}

t_get_strips_quotes() {
  local v; v="$(ENV_PATH="$SAMPLE" env_get_ QUOTED)"
  # Asserts the VALUE, not merely the absence of quotes: "no quotes present" is also true of an
  # empty string, which is how a broken reader passes this kind of check.
  [ "$v" = "quotedvalue" ] || [ "$v" = "quoted value" ] || { echo "got [$v]"; return 1; }
}

t_get_missing_is_empty() {
  local v; v="$(ENV_PATH="$SAMPLE" env_get_ NOT_THERE)"
  [ -z "$v" ] || { echo "got [$v]"; return 1; }
}

t_get_numeric() {
  local v; v="$(ENV_PATH="$SAMPLE" env_get_ NUMBER)"
  [ "$v" = "15432" ] || { echo "got [$v]"; return 1; }
}

t_set_rewrites_in_place() {
  local t; t="$(mktemp)"; cp "$SAMPLE" "$t"
  ENV_PATH="$t" env_set_ PLAIN changed
  grep -qx 'PLAIN=changed' "$t" || { echo "not rewritten"; return 1; }
  grep -q '# a leading comment' "$t" || { echo "leading comment lost"; return 1; }
  [ "$(grep -c '^PLAIN=' "$t")" -eq 1 ] || { echo "key duplicated"; return 1; }
}

t_set_appends_absent() {
  local t; t="$(mktemp)"; cp "$SAMPLE" "$t"
  ENV_PATH="$t" env_set_ BRAND_NEW yes
  grep -qx 'BRAND_NEW=yes' "$t" || { echo "not appended"; return 1; }
  grep -qx 'PLAIN=value' "$t" || { echo "an existing key was disturbed"; return 1; }
}

t_set_ignores_commented() {
  local t; t="$(mktemp)"; cp "$SAMPLE" "$t"
  ENV_PATH="$t" env_set_ COMMENTED_OUT forced
  grep -qx '#COMMENTED_OUT=nope' "$t" || { echo "the commented line was rewritten"; return 1; }
  grep -qx 'COMMENTED_OUT=forced' "$t" || { echo "not appended separately"; return 1; }
}

t_set_url_value() {
  # A URL carries `/` and `:`, which break a naive sed replacement -- the exact value this launcher
  # must write for the split-horizon issuer.
  local t url got; t="$(mktemp)"; cp "$SAMPLE" "$t"
  url='http://authentik-server:9000/application/o/forgeops/'
  ENV_PATH="$t" env_set_ OIDC_ISSUER "$url"
  got="$(ENV_PATH="$t" env_get_ OIDC_ISSUER)"
  [ "$got" = "$url" ] || { echo "wrote [$url] read [$got]"; return 1; }
}

t_set_round_trips_secret() {
  local t s got; t="$(mktemp)"; cp "$SAMPLE" "$t"
  s="$(new_secret_ 24)"
  ENV_PATH="$t" env_set_ ENVELOPE_PEPPER "$s"
  got="$(ENV_PATH="$t" env_get_ ENVELOPE_PEPPER)"
  [ "$got" = "$s" ] || { echo "wrote [$s] read [$got]"; return 1; }
}

t_set_preserves_line_count_on_rewrite() {
  local t before after; t="$(mktemp)"; cp "$SAMPLE" "$t"
  before="$(wc -l < "$t")"
  ENV_PATH="$t" env_set_ PLAIN other
  after="$(wc -l < "$t")"
  [ "$before" = "$after" ] || { echo "line count changed from $before to $after"; return 1; }
}

t_need_secret_logic() {
  local t; t="$(mktemp)"
  printf 'A_KEY=change-me-locally\nB_KEY=already-set\n' > "$t"
  ENV_PATH="$t" need_secret_ MISSING_KEY || { echo "an absent key was not flagged"; return 1; }
  ENV_PATH="$t" need_secret_ A_KEY       || { echo "the placeholder was not flagged"; return 1; }
  if ENV_PATH="$t" need_secret_ B_KEY; then
    echo "a real value was flagged for regeneration"; return 1
  fi
  return 0
}

check 'a plain value is read'                        t_get_plain
check 'a trailing comment is stripped'               t_get_strips_comment
check 'surrounding quotes are stripped'              t_get_strips_quotes
check 'a missing key reads empty'                    t_get_missing_is_empty
check 'a numeric value is read'                      t_get_numeric
check 'an existing key is rewritten in place'        t_set_rewrites_in_place
check 'an absent key is appended'                    t_set_appends_absent
check 'a commented-out key is left alone'            t_set_ignores_commented
check 'a URL value survives the write'               t_set_url_value
check 'a generated secret round-trips'               t_set_round_trips_secret
check 'rewriting does not change the line count'     t_set_preserves_line_count_on_rewrite
check 'secrets are generated only when needed'       t_need_secret_logic

printf '\nFiles and shapes the launcher depends on\n'

t_dep() { [ -e "$REPO_ROOT/$1" ] || { echo "not found"; return 1; }; }
for rel in docker-compose.yml docker-compose.e2e.yml .env.example \
           backend/requirements-dev.lock scripts/init_ca.py \
           scripts/ci/provision-authentik.py scripts/check-oidc-reachability.py; do
  # shellcheck disable=SC2317
  eval "t_dep_$(echo "$rel" | tr './-' '___')() { t_dep '$rel'; }"
  check "depends on $rel" "t_dep_$(echo "$rel" | tr './-' '___')"
done

t_executable_bit() {
  local mode
  mode="$(cd "$REPO_ROOT" && git ls-files -s scripts/start-forgeops.sh | cut -d' ' -f1)"
  [ "$mode" = "100755" ] || { echo "git mode is [$mode], not 100755 -- ./scripts/start-forgeops.sh would not run"; return 1; }
}

t_shebang() {
  head -n 1 "$TARGET" | grep -q '^#!/usr/bin/env bash' || { echo "missing bash shebang"; return 1; }
}

t_no_crlf() {
  # A CRLF line ending makes the shebang `#!/usr/bin/env bash\r`, and Linux reports
  # "bad interpreter: No such file or directory" -- the single most common way a script authored on
  # Windows fails to run on Ubuntu.
  if grep -qU $'\r' "$TARGET"; then echo "the file contains CR characters"; return 1; fi
}

t_compose_services() {
  command -v docker >/dev/null 2>&1 || { echo "docker not available in this image (skipping is not passing)"; return 1; }
  local out
  out="$(cd "$REPO_ROOT" && docker compose -f docker-compose.yml -f docker-compose.e2e.yml config --services 2>&1)" || {
    echo "compose config failed: $out"; return 1; }
  local svc
  for svc in postgres redis opa cerbos authentik-server authentik-worker backend frontend agent; do
    printf '%s\n' "$out" | grep -qx "$svc" || { echo "service '$svc' is not declared"; return 1; }
  done
}

check 'the launcher is executable in git'       t_executable_bit
check 'it declares a bash shebang'              t_shebang
check 'it has no CRLF line endings'             t_no_crlf
check 'every service it starts is declared'     t_compose_services

printf '\n  passed %s, failed %s\n\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
