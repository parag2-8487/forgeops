#!/bin/sh
# Phase 0 area-1 licence and project-identity check.
#
# Enforces design.md §0.3 (project identity), §2.4 (two-licence layout and the
# exact agent/NOTICE base text), §16.6 (SPDX identifier discipline) and
# §17.1 D-14/D-19.
#
# Scope: only the artifacts created by task 1.2 — root LICENSE, agent/LICENSE,
# agent/NOTICE and the root README.md identity/licence wording. Backend
# pyproject.toml metadata (task 2.1), frontend package.json metadata (task 6.1)
# and Go SPDX file headers (task 3.1) are deliberately NOT checked here.
#
# Read-only: it never creates, moves, formats or deletes anything. It prints
# every violation it finds and exits non-zero if there is at least one.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

TMPDIR_BASE=${TMPDIR:-/tmp}
FAILFILE=$(mktemp 2>/dev/null || printf '%s' "$TMPDIR_BASE/forgeops-licence-$$")
WORKFILE=$(mktemp 2>/dev/null || printf '%s' "$TMPDIR_BASE/forgeops-licence-work-$$")
EXPECTFILE=$(mktemp 2>/dev/null || printf '%s' "$TMPDIR_BASE/forgeops-licence-expect-$$")
: >"$FAILFILE"
trap 'rm -f "$FAILFILE" "$WORKFILE" "$EXPECTFILE"' EXIT HUP INT TERM

fail() {
	printf 'FAIL: %s\n' "$1" >&2
	printf 'x\n' >>"$FAILFILE"
}

# normalise <file> -- strips CR so CRLF checkouts compare identically
normalise() {
	tr -d '\r' <"$1"
}

# require_file <path> <description>
require_file() {
	if [ ! -f "$1" ]; then
		fail "$2 is missing: $1"
		return 1
	fi
	return 0
}

# require_text <path> <fixed string> <why>
require_text() {
	if ! normalise "$1" | grep -qF -- "$2"; then
		fail "$1 must contain \"$2\" ($3)"
	fi
}

# forbid_text <path> <fixed string> <why>
forbid_text() {
	if normalise "$1" | grep -qF -- "$2"; then
		fail "$1 must not contain \"$2\" ($3)"
	fi
}

echo 'Checking root LICENSE = FSL-1.1-ALv2 (design §2.4, §17.1 D-19)...'
if require_file LICENSE 'root LICENSE (repository default licence)'; then
	require_text LICENSE 'Functional Source License, Version 1.1, ALv2 Future License' \
		'the licence must identify itself by its registered name'
	require_text LICENSE 'FSL-1.1-ALv2' 'registered SPDX short identifier'
	require_text LICENSE 'Copyright 2026 parag8487' 'copyright owner per design §0.3'
	require_text LICENSE 'Competing Use' 'FSL Permitted Purpose terms must be present'
	require_text LICENSE 'Grant of Future License' 'the Apache 2.0 future-licence grant must be present'
	require_text LICENSE 'second anniversary' 'the two-year Apache 2.0 conversion must be stated'
	require_text LICENSE 'Apache License, Version 2.0' 'the future licence is Apache-2.0'
	forbid_text LICENSE 'FSL-1.1-Apache-2.0' \
		'unregistered alias; design §2.4 forbids it — use FSL-1.1-ALv2'
	forbid_text LICENSE 'Business Source License' 'D-19 selected FSL, not BSL 1.1'
	# The root licence must not be an Apache-2.0 copy by mistake.
	if normalise LICENSE | grep -qF 'TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION'; then
		fail 'root LICENSE looks like the Apache-2.0 text; it must be FSL-1.1-ALv2 (design §2.4)'
	fi
fi

echo 'Checking agent/LICENSE = Apache-2.0 (design §2.4, NFR-31)...'
if require_file agent/LICENSE 'agent/LICENSE (Apache-2.0 for the agent and CLI subtree)'; then
	require_text agent/LICENSE 'Apache License' 'the agent subtree is Apache-2.0'
	require_text agent/LICENSE 'Version 2.0, January 2004' 'the full Apache-2.0 text is required'
	require_text agent/LICENSE 'TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION' \
		'the full Apache-2.0 terms must be present, not a reference'
	require_text agent/LICENSE 'END OF TERMS AND CONDITIONS' 'the Apache-2.0 text must be complete'
	require_text agent/LICENSE 'Copyright 2026 parag8487' 'copyright owner per design §0.3'
	forbid_text agent/LICENSE 'FSL-1.1-ALv2' 'the agent subtree is Apache-2.0 only (design §2.4)'
	forbid_text agent/LICENSE 'Functional Source License' 'the agent subtree is Apache-2.0 only (design §2.4)'
fi

echo 'Checking agent/NOTICE base text exactly (design §2.4, §17.1 D-19)...'
if require_file agent/NOTICE 'agent/NOTICE (complete Apache project notice)'; then
	cat >"$EXPECTFILE" <<'EOF'
ForgeOps Agent
Copyright 2026 parag8487

This product includes software developed by the ForgeOps project.
The ForgeOps Agent and CLI are licensed under the Apache License, Version 2.0.
See the adjacent LICENSE file for the complete license terms.
EOF
	# Compare only the base block: the first six lines of NOTICE must match the
	# design §2.4 text byte-for-byte. Later tasks may append audited upstream
	# notices beneath it (task 15.5); they must not alter the base block.
	normalise agent/NOTICE | sed -n '1,6p' >"$WORKFILE"
	if ! diff -u "$EXPECTFILE" "$WORKFILE" >/dev/null 2>&1; then
		fail 'agent/NOTICE base text does not match design §2.4 exactly'
		diff -u "$EXPECTFILE" "$WORKFILE" >&2 2>/dev/null || true
	fi

	# No placeholder or prospective attribution is permitted (design §2.4).
	for forbidden in TODO FIXME XXX 'TBD' 'stub' 'Stub' 'STUB' 'placeholder' 'Placeholder' \
		'PLACEHOLDER' 'to be added' 'To be added' 'to be determined' 'coming soon' \
		'will be added' 'if required' '<' '>'; do
		if normalise agent/NOTICE | grep -qF -- "$forbidden"; then
			fail "agent/NOTICE must not contain placeholder or prospective text: \"$forbidden\" (design §2.4)"
		fi
	done

	# An "Upstream notices" heading is allowed only when real notice text follows.
	if normalise agent/NOTICE | grep -qF 'Upstream notices'; then
		UPSTREAM_BODY=$(normalise agent/NOTICE | sed -n '/Upstream notices/,$p' | sed '1d' | tr -d ' \t\n')
		if [ -z "$UPSTREAM_BODY" ]; then
			fail 'agent/NOTICE has an empty "Upstream notices" heading; omit the heading instead (design §2.4)'
		fi
	fi
fi

echo 'Checking root README.md identity and licence wording (design §0.3, §2.4)...'
if require_file README.md 'root README.md (project identity and licence split)'; then
	require_text README.md 'ForgeOps' 'project name (design §0.3, D-14)'
	require_text README.md 'https://github.com/parag8487/ForgeOps' 'repository URL (design §0.3)'
	require_text README.md 'parag8487' 'owner (design §0.3)'
	require_text README.md 'github.com/parag8487/ForgeOps/agent' 'Go module path (D-14)'

	# The licence split must be stated, not implied.
	if ! normalise README.md | grep -qiE '^#+[[:space:]]+licen[cs]e'; then
		fail 'README.md must carry a Licence section (design §2.4)'
	fi
	require_text README.md 'FSL-1.1-ALv2' 'repository-default SPDX identifier (design §2.4)'
	require_text README.md 'Apache-2.0' 'agent subtree SPDX identifier (design §2.4)'
	require_text README.md 'Functional Source License 1.1, Apache 2.0 future licence' \
		'prose licence name (design §2.4)'
	require_text README.md 'agent/' 'the path the Apache-2.0 licence covers (design §2.4)'
	require_text README.md 'source-available' \
		'the platform must be described as source-available, not open source (D-19 residual note)'
	if ! normalise README.md | grep -qiE 'second anniversary|after two years|two-year'; then
		fail 'README.md must state the two-year Apache 2.0 conversion (design §2.4, D-19)'
	fi
	if ! normalise README.md | grep -qiE 'not[^.]{0,40}OSI-approved'; then
		fail 'README.md must state that the FSL is not an OSI-approved open-source licence (D-19)'
	fi

	forbid_text README.md 'FSL-1.1-Apache-2.0' \
		'unregistered alias; design §2.4 forbids it — use FSL-1.1-ALv2'
	# The backend/platform must never be advertised as open source (D-19).
	if normalise README.md |
		grep -qiE '(fully|entirely|completely)[[:space:]]+open[ -]source|open[ -]source[[:space:]]+(platform|backend)|backend[^.]{0,60}open[ -]source[^.]{0,20}(platform|project|$)'; then
		fail 'README.md must not describe the platform or backend as open source (design §2.4, D-19)'
	fi
fi

VIOLATIONS=$(wc -l <"$FAILFILE" | tr -d ' \t')
if [ "${VIOLATIONS:-0}" -ne 0 ]; then
	printf '\nlicence/identity check failed with %s violation(s)\n' "$VIOLATIONS" >&2
	exit 1
fi

echo 'licence/identity check passed'
exit 0
