#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/check-coverage.sh — the agent's third of criterion 11: statement coverage over
# `./internal/...` must reach a threshold, or the build fails (design.md §7.13, D-31).
#
# THIS FILE DID NOT EXIST until 2026-08-21. PROGRESS.md's criterion 11 cited it by name —
# "`agent` runs `scripts/check-coverage.sh 70` over `./internal/...`" — as evidence that the
# gate was on and green, for a script that was in neither the working tree nor the index. Two
# other thirds of the same criterion were absent the same way. LEARNING-JOURNAL finding 81
# records the shape: a citation is not an implementation, and a filename in a document is the
# cheapest possible way to look finished.
#
# THE THRESHOLD IS AN ARGUMENT, NOT A DEFAULT, and there is no fallback value. A default would
# let a caller invoke this with no argument and get a pass from whatever number the author
# happened to choose, which is how a gate becomes decorative. `make` and CI both pass 70
# explicitly, so the number is visible at the call site.
#
# WHY `go test -coverprofile` OVER ONE PROFILE rather than per-package percentages: `go test`
# prints a per-package figure, and averaging those is wrong — it weights a 4-line package the
# same as a 400-line one. `go tool cover -func` on a single merged profile reports a `total:`
# line computed over all statements in scope, which is the figure the criterion means.
#
# `-coverpkg` is set to the same pattern as the test scope so that a package covered ONLY by
# another package's tests still counts. Without it, a package with no _test.go file of its own
# is simply absent from the profile rather than counted as uncovered, and the total flatters
# itself by ignoring exactly the code most likely to be untested (the §0.4.5 vacuity trap).
set -euo pipefail

THRESHOLD="${1:-}"
PATTERN="${2:-./internal/...}"

if [ -z "$THRESHOLD" ]; then
	printf 'usage: %s <threshold-percent> [package-pattern]\n' "$0" >&2
	printf '  the threshold is required; this script has no default so the gate cannot\n' >&2
	printf '  silently pass at a number nobody chose\n' >&2
	exit 2
fi

case "$THRESHOLD" in
*[!0-9]* | '')
	printf 'ERROR: threshold must be a whole number of percent, got: %s\n' "$THRESHOLD" >&2
	exit 2
	;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR="$REPO_ROOT/agent"

if [ ! -f "$AGENT_DIR/go.mod" ]; then
	printf 'ERROR: no go.mod at %s\n' "$AGENT_DIR" >&2
	exit 1
fi

printf '==> check-coverage: %s over %s, threshold %s%%\n' "$AGENT_DIR" "$PATTERN" "$THRESHOLD"

PROFILE="$(mktemp -t forgeops-cover.XXXXXX)"
trap 'rm -f "$PROFILE"' EXIT

cd "$AGENT_DIR"
go test -covermode=atomic -coverpkg="$PATTERN" -coverprofile="$PROFILE" "$PATTERN"

# `go tool cover -func` ends with a line of the form:
#   total:	(statements)	83.4%
TOTAL_LINE="$(go tool cover -func="$PROFILE" | tail -n 1)"
COVERAGE="$(printf '%s\n' "$TOTAL_LINE" | awk '{ gsub(/%/, "", $NF); print $NF }')"

if [ -z "$COVERAGE" ]; then
	printf 'ERROR: could not parse a total out of: %s\n' "$TOTAL_LINE" >&2
	printf '  refusing to pass on an unreadable profile\n' >&2
	exit 1
fi

printf '==> check-coverage: total %s%% (threshold %s%%)\n' "$COVERAGE" "$THRESHOLD"

# Integer comparison on the truncated percentage. awk rather than bash arithmetic because the
# figure is fractional, and `[ 69.9 -ge 70 ]` is a syntax error rather than a false.
if awk -v c="$COVERAGE" -v t="$THRESHOLD" 'BEGIN { exit !(c + 0 < t + 0) }'; then
	printf 'FAIL: agent coverage %s%% is below the required %s%%\n' "$COVERAGE" "$THRESHOLD" >&2
	printf '  raise the coverage. Do not lower the threshold: a gate tuned to what the code\n' >&2
	printf '  currently achieves measures nothing (criterion 11, D-31).\n' >&2
	exit 1
fi

printf 'ok:   agent coverage %s%% meets the %s%% gate\n' "$COVERAGE" "$THRESHOLD"
