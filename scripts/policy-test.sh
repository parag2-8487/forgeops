#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# The policy gate: `opa check --strict` and `opa test` over every Rego bundle in the
# repository. design.md §8.3 (the `policy` CI job), §11.7, §13.4 (`make policy-test`);
# task 9.1; criterion 7.
#
# ONE DEFINITION, TWO CALLERS. `make policy-test` and the `policy` CI job both run this
# file, so the gate a developer sees locally is byte-identical to the one that blocks a
# pull request. A workflow that inlined the two commands would be a second definition,
# and the journal already records what happens to those.
#
# THE OPA VERSION IS READ OUT OF docker-compose.yml, NOT RESTATED. §10.6.1 requires the
# agent's embedded evaluator, the backend's OPA server and this gate to run the SAME Rego
# semantics; the compose file is the one place that pin is policed (check-no-latest.sh,
# check-compose-validate.py). `scripts/ci/start-cerbos.sh` reads its image the same way
# and for the same reason. A local binary is used only when its version matches that pin
# exactly — otherwise the container is used, so a developer with an older OPA on PATH gets
# the real answer rather than a locally-green, remotely-red one.
#
# WHY `--ignore '*.yaml'` (D-91). `policies/` holds two unrelated policy systems: Rego for
# OPA and YAML for Cerbos (leaf 6.4). OPA loads a `.yaml` under a `-d` root as a DATA
# document, and the six Cerbos files all define a top-level `apiVersion`, so loading them
# together is six `merge error`s and the whole run dies before a single test executes.
# §8.3's literal command is therefore `opa test policies/ -v` plus this flag. The scope
# stays the whole `policies/` tree rather than an enumerated list of Rego subdirectories,
# so a third bundle added later is covered the day it lands instead of the day someone
# remembers to add it here.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${repo_root}/docker-compose.yml"
cd "$repo_root"

image="$(grep -oE 'openpolicyagent/opa:[^ ]+@sha256:[0-9a-f]{64}' "$compose_file" | head -n 1)"
if [ -z "$image" ]; then
  echo "FAIL: no digest-pinned openpolicyagent/opa reference found in ${compose_file}" >&2
  echo "FAIL: this script reads the pin rather than restating it, so a missing" >&2
  echo "FAIL: reference is a real problem and not something to work around." >&2
  exit 1
fi
# openpolicyagent/opa:1.4.2@sha256:... -> 1.4.2
pinned_version="${image#*:}"
pinned_version="${pinned_version%%@*}"
echo "pinned OPA: ${pinned_version} (${image})"

# ─── Pick a runner: matching local binary, else the pinned container ───────────
opa_cmd=()
if command -v opa >/dev/null 2>&1; then
  local_version="$(opa version 2>/dev/null | awk '/^Version:/ {print $2}')"
  if [ "$local_version" = "$pinned_version" ]; then
    echo "using the local opa binary (${local_version}), which matches the pin"
    opa_cmd=(opa)
  else
    echo "local opa is ${local_version:-unknown}, pin is ${pinned_version}; using the container"
  fi
fi

if [ "${#opa_cmd[@]}" -eq 0 ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "FAIL: no opa binary at version ${pinned_version} and no docker to run the pinned image." >&2
    echo "FAIL: install OPA ${pinned_version} or start Docker. This gate is not skippable:" >&2
    echo "FAIL: criterion 7 has no other evidence." >&2
    exit 1
  fi
  # MSYS_NO_PATHCONV keeps Git Bash on Windows from rewriting the container-side paths,
  # the same guard scripts/lib/pip-compile.sh uses.
  opa_cmd=(env MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm
    -v "${repo_root}/policies:/policies:ro" -w / "$image")
  policies_root="/policies"
else
  policies_root="policies"
fi

# ─── Clause 1: every bundle compiles under --strict ───────────────────────────
echo "==> opa check --strict --ignore '*.yaml' ${policies_root}"
"${opa_cmd[@]}" check --strict --ignore '*.yaml' "$policies_root"

# ─── Clause 2: every bundle's tests pass, and there is more than nothing ──────
#
# `opa test` over a tree with no test files prints "PASS: 0/0" and exits 0. A gate that
# reports success for an empty selection is the shape D-51 rejects, so the count is read
# out of the output and required to be non-zero.
echo "==> opa test ${policies_root} --ignore '*.yaml' -v"
test_output="$("${opa_cmd[@]}" test "$policies_root" --ignore '*.yaml' -v 2>&1)"
echo "$test_output"

summary="$(printf '%s\n' "$test_output" | grep -oE 'PASS: [0-9]+/[0-9]+' | tail -n 1 || true)"
if [ -z "$summary" ]; then
  echo "FAIL: opa test printed no PASS summary; the run did not complete" >&2
  exit 1
fi
passed="${summary#PASS: }"
passed="${passed%%/*}"
if [ "$passed" -lt 1 ]; then
  echo "FAIL: opa test selected no tests (${summary}). A green run over an empty" >&2
  echo "FAIL: selection is not evidence." >&2
  exit 1
fi
echo "opa test: ${summary}"

# ─── Clause 3: every governance bundle file is total at its entry document ────
#
# Task 9.1's own wording: each file in the governance bundle carries an explicit
# fail-closed default so a deny is a DEFINED false. `governance_test.rego` asserts
# the behaviour; this asserts the DECLARATION, because the behavioural assertion can be
# satisfied by accident for an input that happens to make the rule fire, while D-25's trap
# is about the input that makes NO rule fire. Both halves are cheap; only one of them
# notices a file added tomorrow without the default.
#
# THE CHECK IS ON THE PROPERTY, NOT ON ONE RULE NAME. It used to require the literal
# `default allow := false` in every file, which is the right property for the files whose entry document
# is `allow` and wrong for a sub-policy whose entry document is called something else. `exemption.rego`
# answers `applies`, is total (`default applies := false`), fails closed in the direction that means "not
# exempt" — and was refused, because the string did not match.
#
# Adding a dead `default allow := false` to it would have satisfied the letter and not the spirit, and
# would have left an `allow` rule in an exemption package for someone to query by mistake. So both halves
# of the real requirement are now stated:
#
#   1. EVERY file declares at least one total fail-closed default. No file may be partial.
#   2. Any file that defines an `allow` rule must default it to false specifically. This is the clause
#      D-25 is about, and it is unchanged for every file it applied to before.
echo "==> every policies/agent/*.rego is total at its entry document and fails closed"
bundle_files=()
while IFS= read -r f; do bundle_files+=("$f"); done < <(
  find policies/agent -name '*.rego' -not -name '*_test.rego' | sort
)
if [ "${#bundle_files[@]}" -eq 0 ]; then
  echo "FAIL: policies/agent holds no non-test .rego file; nothing was checked" >&2
  exit 1
fi
missing=0
for f in "${bundle_files[@]}"; do
  # A total fail-closed default of any name: `default allow := false`, `default applies := false`,
  # `default result := "deny"`, `default reason := ""`.
  if ! grep -qE '^default [a-z_][a-z0-9_]* :=' "$f"; then
    echo "  [MISSING] $f declares no total default, so it can answer as an UNDEFINED document" >&2
    missing=1
    continue
  fi
  # The D-25 clause, unchanged: a file that HAS an `allow` must default it to false.
  if grep -qE '^allow( |\[)' "$f" || grep -qE '^default allow' "$f"; then
    if grep -qE '^default allow := false$' "$f"; then
      echo "  [ok]      $f (allow defaults to false)"
    else
      echo "  [MISSING] $f defines 'allow' without 'default allow := false'" >&2
      missing=1
    fi
  else
    # Named so a reader can see WHICH document was accepted as the entry point, rather than trusting
    # that some default existed somewhere in the file.
    entry=$(grep -oE '^default [a-z_][a-z0-9_]*' "$f" | head -n 1 | awk '{print $2}')
    echo "  [ok]      $f (no 'allow'; total at 'default ${entry}')"
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "FAIL: a governance bundle file can answer a deny as an UNDEFINED document." >&2
  echo "FAIL: That is byte-identical over HTTP to a bundle that failed to load (D-25)." >&2
  exit 1
fi
echo "policy gate: ${#bundle_files[@]} bundle file(s) total, ${summary}"
