#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/go-tool.sh — build a pinned Go tool from agent/tools and run it in the
# caller's working directory (design.md §0.5 debt D4, §16.1).
#
# Usage:
#   scripts/go-tool.sh <tool-package> [args...]
#   scripts/go-tool.sh github.com/golangci/golangci-lint/cmd/golangci-lint run ./...
#
# Why build rather than `go run`
# ------------------------------
# `go run` executes with the tool module as the main module, so any relative package
# pattern the tool receives (`./...`) resolves against `agent/tools` instead of the
# module under analysis. Both golangci-lint and govulncheck take such patterns, and
# the failure is confusing rather than loud: "directory prefix .. does not contain
# main module". Building to a temp path and executing from the caller's directory
# keeps the tool pinned by go.sum and the pattern meaningful.
#
# The version is whatever agent/tools/go.mod pins, verified against that module's
# committed go.sum — never resolved at run time.
set -euo pipefail

if [ "$#" -lt 1 ]; then
	printf 'usage: %s <tool-package> [args...]\n' "$0" >&2
	exit 2
fi

TOOL_PKG="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOOLS_DIR="$SCRIPT_DIR/../agent/tools"

if [ ! -f "$TOOLS_DIR/go.mod" ] || [ ! -f "$TOOLS_DIR/go.sum" ]; then
	printf 'ERROR: agent/tools needs both go.mod and go.sum; that pair is what pins the tool\n' >&2
	exit 1
fi

BINDIR="$(mktemp -d)"
trap 'rm -rf "$BINDIR"' EXIT INT TERM

BIN="$BINDIR/$(basename "$TOOL_PKG")"
(cd "$TOOLS_DIR" && go build -o "$BIN" "$TOOL_PKG")

exec "$BIN" "$@"
