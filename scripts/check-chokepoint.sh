#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/check-chokepoint.sh - the chokepoint reachability check (design.md 2.2.1, 11.6, Q-03).
#
# WHAT IT ASSERTS
#   Go:     no package outside internal/executor/** imports
#           internal/executor/internal/mutate, read from `go list -deps -json ./...`.
#   Python: every call to a @mutation_primitive-decorated function under backend/src/** is
#           lexically inside src/governance/ or receives a MutationAuthority.
#
# WHY IT EXISTS WHEN TWO STRONGER MECHANISMS ALREADY DO
#   Go's nested-`internal` rule is a COMPILE-time boundary and MutationAuthority cannot be
#   constructed outside governance/. Neither survives a well-meaning refactor that moves a
#   package INSIDE the executor subtree, or a primitive that grows a caller nobody notices.
#   The two enumerations here are derived from the tree rather than hand-listed, so a newly
#   marked primitive and a newly added package are both covered without editing this file.
#
# VACUITY
#   Exit 0 only when BOTH enumerations are non-empty and clean. An empty primitive set is a
#   hard failure - a renamed decorator must not make the check trivially pass - and so is an
#   import graph that does not contain the boundary package at all. An empty *importer* set is
#   reported, not failed: only `executor` itself may import the boundary and its dispatcher
#   arrives in leaf 8.7, so zero is the correct answer today.
#
# INVOCATION
#   bash scripts/check-chokepoint.sh          # both halves
#   bash scripts/check-chokepoint.sh --python # one half
#   bash scripts/check-chokepoint.sh --go
#
# Also run by `make lint`, by the `agent` and `backend` CI jobs, and by a pre-commit local hook
# on ^(agent|backend)/.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

HALF="both"
case "${1:-}" in
"--python") HALF="python" ;;
"--go") HALF="go" ;;
"--both" | "") HALF="both" ;;
*)
	printf 'usage: %s [--python|--go|--both]\n' "$0" >&2
	exit 2
	;;
esac

# The interpreter, in the order a developer and CI actually have one. The project venv first,
# because that is what every other backend check uses; then the ambient python3.
PYBIN=""
for candidate in \
	"$REPO_ROOT/backend/.venv/bin/python" \
	"$REPO_ROOT/backend/.venv/Scripts/python.exe" \
	python3 python; do
	if command -v "$candidate" >/dev/null 2>&1; then
		PYBIN="$candidate"
		break
	fi
done
if [ -z "$PYBIN" ]; then
	printf 'check-chokepoint: no python interpreter found\n' >&2
	exit 2
fi

printf '==> check-chokepoint: %s half/halves via %s\n' "$HALF" "$(basename "$PYBIN")"

# The Go half needs the toolchain. Its absence is a hard failure rather than a skip: a check
# that silently degrades to half its assertions is the "gate that could never fail" shape
# design 0.4.4 and D-51 both reject.
if [ "$HALF" != "python" ] && ! command -v go >/dev/null 2>&1; then
	printf 'check-chokepoint: the Go half needs the go toolchain and it is not on PATH.\n' >&2
	printf '  Install Go, or run `bash scripts/check-chokepoint.sh --python` and say so.\n' >&2
	exit 2
fi

"$PYBIN" "$SCRIPT_DIR/chokepoint_graph.py" \
	--half "$HALF" \
	--src "$REPO_ROOT/backend/src" \
	--agent "$REPO_ROOT/agent"
RC=$?

if [ "$RC" -ne 0 ]; then
	printf '\ncheck-chokepoint: FAILED (exit %s). See design.md 2.2.1 and Appendix B Q-03.\n' "$RC" >&2
	exit "$RC"
fi
printf 'check-chokepoint: both halves clean\n'
