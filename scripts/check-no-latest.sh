#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/check-no-latest.sh — no floating tool version anywhere (design.md §0.5
# debt D4, §8.4, §16.1).
#
# Why
# ---
# Phase 0 installed the vulnerability scanner itself with
# `go install golang.org/x/vuln/cmd/govulncheck@latest`, and the frontend image ran
# `corepack prepare pnpm@latest`. Both resolve at build time to whatever the registry
# serves, verified against nothing — so the gate that is supposed to prove the
# dependency set is safe was itself unpinned, and two builds of the same commit could
# use different tools. Phase 1 adds ~14 Go and ~6 frontend dependencies, so the
# posture has to tighten as the surface grows, not drift.
#
# Scope: every workflow, script, Dockerfile, Makefile and compose file tracked by git.
# Only tracked files are scanned, so a developer's scratch cannot fail the build and a
# committed regression always can.
#
# Failure is exit 1 listing `path:line: <matched text>`.
#
# Exit 1 also when the scanned file set is EMPTY: a glob that stopped matching would
# otherwise pass forever, the same vacuity trap §0.4.5 closes for the mutation harness.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Patterns that mean "resolve this at build time", not "use this exact thing".
#   @latest          go install / npm / corepack
#   :latest          container image tag
#
# `@*` was tried and removed: it matched `${PIP_COMPILE_IMAGE%%@*}`, an ordinary
# shell parameter expansion, and a check that reports work you do not have to do gets
# switched off. An npm `@*` range is rare enough that catching it is not worth that.
FLOATING='(@latest\b|:latest\b)'

# A line may opt out with `allow-floating: <reason>`. A reason is required, because a
# bare escape hatch is how a real regression gets waved through — the same rule
# scripts/check-test-doubles.py applies to `# noqa`.
SUPPRESSION='allow-floating:[[:space:]]*[^[:space:]]'

mapfile -t FILES < <(
	git ls-files -z \
		'.github/**' \
		'scripts/**' \
		'*Dockerfile' \
		'**/Dockerfile' \
		'Makefile' \
		'docker-compose*.yml' |
		tr '\0' '\n' | grep -v '^$'
)

if [ "${#FILES[@]}" -eq 0 ]; then
	printf 'ERROR: check-no-latest scanned no files; the globs match nothing\n' >&2
	exit 1
fi

printf '==> check-no-latest: %d tracked file(s)\n' "${#FILES[@]}"

FOUND=0
for file in "${FILES[@]}"; do
	[ -f "$file" ] || continue
	# This script necessarily contains the pattern it looks for, so it excludes
	# itself by name rather than by a magic comment a future edit could drop.
	if [ "$file" = "scripts/check-no-latest.sh" ]; then
		continue
	fi
	while IFS=: read -r line text; do
		[ -n "${line:-}" ] || continue
		if printf '%s' "$text" | grep -qE "$SUPPRESSION"; then
			continue
		fi
		printf '%s:%s: %s\n' "$file" "$line" "$(printf '%s' "$text" | sed 's/^[[:space:]]*//')" >&2
		FOUND=1
	done < <(grep -nE "$FLOATING" "$file" || true)
done

if [ "$FOUND" -ne 0 ]; then
	cat >&2 <<'EOF'

ERROR: a floating version reference was found.

Pin it instead:
  - Go tools:      add the module to agent/tools/go.mod and invoke it with
                   `go run <module>/cmd/<tool>`, so go.sum verifies the checksum
  - container tag: use tag@sha256:<digest>
  - pnpm/corepack: name the exact version, e.g. `corepack prepare pnpm@10.x.y`
EOF
	exit 1
fi

printf 'check-no-latest: no floating version references\n'
