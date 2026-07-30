#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/check-go-interface-assertions.sh — every concrete type that satisfies a
# project interface must carry `var _ Iface = (*Impl)(nil)` in a test file
# (design.md §0.4.2, §8.3, §9).
#
# Why: a compile-time assertion cannot rot, because the compiler rechecks it on
# every build — but it can be ABSENT, and an absent assertion is indistinguishable
# from a satisfied one right up to the day a signature changes and only the
# injection site breaks, at runtime. That is the Go shape of the Phase 0 D-23
# defect. This closes it.
#
# The analysis itself lives in scripts/go/ifacecheck, a standalone module with an
# empty require list: it uses `go list -export` data and stdlib `go/types`, so
# nothing enters agent/go.mod, which D-1's cgo guard and the release SBOM police.
#
# Failure is exit 1 naming the interface and the unasserted implementation, and
# also exit 1 when the discovered interface set is EMPTY — a checker that finds
# nothing would otherwise pass forever (the §0.4.5 vacuity trap).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR="${1:-$REPO_ROOT/agent}"
PATTERN="${2:-./internal/...}"
TOOL_DIR="$SCRIPT_DIR/go/ifacecheck"

if [ ! -d "$AGENT_DIR" ]; then
	printf 'ERROR: agent module directory not found: %s\n' "$AGENT_DIR" >&2
	exit 1
fi

printf '==> check-go-interface-assertions: %s %s\n' "$AGENT_DIR" "$PATTERN"

# `go run` needs to be invoked from inside the tool's module; -dir carries the
# module under audit as an absolute path.
cd "$TOOL_DIR"
exec go run . -dir "$AGENT_DIR" -pkgs "$PATTERN"
