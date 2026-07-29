#!/bin/sh
# Phase 0 Makefile contract check (design.md §13.4, §16.2, §16.4).
#
# Enumerates the declared targets and verifies the initial Phase 0 contract:
#   * help is the default goal and every target is .PHONY
#   * bootstrap, init-env and clean exist
#   * init-env delegates to scripts/init-env.sh (which may not exist yet)
#   * bootstrap verifies the pinned Docker Compose / pip-tools versions and
#     never rewrites lockfiles
#   * clean never deletes .env, any lockfile, or a Docker volume
#
# Every inspection is static or a `make --dry-run` expansion, so no long-lived
# command (bootstrap, up, logs, watchers) is ever executed.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

MAKEFILE=Makefile
REQUIRED_TARGETS='help
bootstrap
init-env
clean'

FAILURES=0

fail() {
	printf 'FAIL: %s\n' "$1" >&2
	FAILURES=$((FAILURES + 1))
}

ok() {
	printf 'ok:   %s\n' "$1"
}

if [ ! -f "$MAKEFILE" ]; then
	printf 'FAIL: root Makefile is missing (design.md §13.4)\n' >&2
	exit 1
fi

MAKE_BIN=${MAKE:-make}
if ! command -v "$MAKE_BIN" >/dev/null 2>&1; then
	printf 'FAIL: %s not found on PATH; GNU make is required to check the contract\n' "$MAKE_BIN" >&2
	exit 1
fi

# --- Target enumeration ------------------------------------------------------
DECLARED_TARGETS=$(awk '
	/^[A-Za-z0-9_.\/-]+[ \t]*:([^=:]|$)/ {
		split($0, parts, ":")
		n = split(parts[1], names, /[ \t]+/)
		for (i = 1; i <= n; i++) {
			if (names[i] != "" && names[i] !~ /^\./) print names[i]
		}
	}
' "$MAKEFILE" | sort -u)

PHONY_TARGETS=$(awk '
	/^\.PHONY[ \t]*:/ {
		sub(/^\.PHONY[ \t]*:[ \t]*/, "")
		print
	}
' "$MAKEFILE" | tr ' \t' '\n\n' | sed '/^$/d' | sort -u)

printf 'Declared targets: %s\n' "$(printf '%s' "$DECLARED_TARGETS" | tr '\n' ' ')"

contains_line() {
	printf '%s\n' "$2" | grep -qx -- "$1"
}

for target in $REQUIRED_TARGETS; do
	if contains_line "$target" "$DECLARED_TARGETS"; then
		ok "target declared: $target"
	else
		fail "required target is missing from the Makefile: $target"
	fi
	if contains_line "$target" "$PHONY_TARGETS"; then
		ok ".PHONY declares: $target"
	else
		fail "target is not declared .PHONY (design.md §13.4): $target"
	fi
done

# Every declared target must be phony in Phase 0 (no file targets exist yet).
for target in $DECLARED_TARGETS; do
	contains_line "$target" "$PHONY_TARGETS" ||
		fail "declared target is not listed in .PHONY: $target"
done

# --- Default goal ------------------------------------------------------------
if grep -Eq '^[[:space:]]*\.DEFAULT_GOAL[[:space:]]*:=[[:space:]]*help[[:space:]]*$' "$MAKEFILE"; then
	ok '.DEFAULT_GOAL is help'
else
	fail '.DEFAULT_GOAL must be set to help (design.md §13.4)'
fi

if default_dry=$("$MAKE_BIN" -n --no-print-directory 2>/dev/null); then
	if printf '%s\n' "$default_dry" | grep -q 'awk'; then
		ok 'default goal expands to the help recipe'
	else
		fail 'the default goal does not expand to the help recipe'
	fi
else
	fail 'make --dry-run with no goal failed'
fi

# --- init-env delegation (script may be added by its owning task) ------------
if init_dry=$("$MAKE_BIN" -n --no-print-directory init-env 2>/dev/null); then
	if printf '%s\n' "$init_dry" | grep -q 'scripts/init-env\.sh'; then
		ok 'init-env delegates to scripts/init-env.sh'
	else
		fail 'init-env must invoke scripts/init-env.sh (design.md §13.4)'
	fi
else
	fail 'make --dry-run init-env failed'
fi

if [ -f scripts/init-env.sh ]; then
	ok 'scripts/init-env.sh is present'
else
	printf 'note: scripts/init-env.sh is not present yet; it is owned by its own task\n'
fi

# --- bootstrap contract ------------------------------------------------------
if bootstrap_dry=$("$MAKE_BIN" -n --no-print-directory bootstrap 2>/dev/null); then
	ok 'make --dry-run bootstrap expanded without executing it'
else
	fail 'make --dry-run bootstrap failed'
fi

for pin in '2\.24\.7' '7\.6\.0'; do
	if grep -Eq "$pin" "$MAKEFILE"; then
		ok "Makefile pins version matching $pin"
	else
		fail "Makefile must state the pinned version matching $pin (design.md §16.2, §16.4)"
	fi
done

if [ -f scripts/bootstrap.sh ]; then
	if grep -q 'pip-tools==' scripts/bootstrap.sh && grep -q '7\.6\.0' scripts/bootstrap.sh; then
		ok 'bootstrap verifies pip-tools==7.6.0'
	else
		fail 'bootstrap must verify pip-tools==7.6.0 (design.md §16.2)'
	fi
	if grep -q 'docker compose version' scripts/bootstrap.sh && grep -q '2\.24\.7' scripts/bootstrap.sh; then
		ok 'bootstrap verifies Docker Compose 2.24.7'
	else
		fail 'bootstrap must verify Docker Compose 2.24.7 (design.md §16.4)'
	fi
	if grep -vE '^[[:space:]]*#' scripts/bootstrap.sh |
		grep -Eq 'pip-compile|pip-sync|go mod tidy|pnpm install|tofu +providers +lock'; then
		fail 'bootstrap must not run a lock-generating command (design.md §13.4)'
	else
		ok 'bootstrap runs no lock-generating command'
	fi
else
	fail 'scripts/bootstrap.sh is missing; bootstrap has no verification logic'
fi

# --- clean exclusions --------------------------------------------------------
if clean_dry=$("$MAKE_BIN" -n --no-print-directory clean 2>/dev/null); then
	ok 'make --dry-run clean expanded without executing it'
	if printf '%s\n' "$clean_dry" | grep -Eq '(^|[[:space:]/])\.env([[:space:]]|$)'; then
		fail 'clean must never remove .env (design.md §13.4)'
	else
		ok 'clean does not touch .env'
	fi
	if printf '%s\n' "$clean_dry" | grep -Eq 'requirements(-dev)?\.lock|pnpm-lock\.yaml|go\.sum|\.terraform\.lock\.hcl|poetry\.lock|uv\.lock'; then
		fail 'clean must never remove a lockfile (design.md §13.4)'
	else
		ok 'clean does not touch any lockfile'
	fi
	if printf '%s\n' "$clean_dry" | grep -Eq 'docker (compose|volume|system)|down .*-v|--volumes'; then
		fail 'clean must never remove Docker volumes (design.md §13.4)'
	else
		ok 'clean does not touch Docker volumes'
	fi
else
	fail 'make --dry-run clean failed'
fi

# --- Authoritative documents stay untouched ---------------------------------
if grep -Eq 'AI-Powered-DevOps-Platform-Complete-Technical-Research\.md|(^|[^-])PRD\.md|Tech-Stack-Analysis\.md|phases\.md' "$MAKEFILE"; then
	fail 'the Makefile must not reference the authoritative root documents (design.md §0.3)'
else
	ok 'no authoritative root document is referenced by the Makefile'
fi

if [ "$FAILURES" -ne 0 ]; then
	printf '\nMakefile contract check failed with %s violation(s)\n' "$FAILURES" >&2
	exit 1
fi

printf '\nMakefile contract check passed\n'
exit 0
