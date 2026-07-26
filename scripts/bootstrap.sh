#!/bin/sh
# Phase 0 toolchain verification for `make bootstrap` (design.md §13.4, §16.2, §16.4).
#
# Verifies the pinned developer toolchain and reports exact remediation for
# anything missing or mismatched. It deliberately never regenerates, rewrites or
# touches any lockfile (requirements.lock, requirements-dev.lock,
# pnpm-lock.yaml, go.sum, .terraform.lock.hcl) and never runs pip-compile.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

DOCKER_COMPOSE_VERSION=${FORGEOPS_DOCKER_COMPOSE_VERSION:-2.24.7}
PIP_TOOLS_VERSION=${FORGEOPS_PIP_TOOLS_VERSION:-7.4.1}

FAILURES=0

fail() {
	printf 'FAIL: %s\n' "$1" >&2
	FAILURES=$((FAILURES + 1))
}

ok() {
	printf 'ok:   %s\n' "$1"
}

have() {
	command -v "$1" >/dev/null 2>&1
}

# --- POSIX shell + GNU make prerequisites -----------------------------------
if have make; then
	if make --version 2>/dev/null | head -n 1 | grep -q 'GNU Make'; then
		ok "GNU make present: $(make --version 2>/dev/null | head -n 1)"
	else
		fail 'GNU make is required (see docs/development.md); a non-GNU make was found'
	fi
else
	fail 'make not found on PATH; install GNU make (Git Bash or WSL2 on Windows)'
fi

# --- Docker Compose exact pin ------------------------------------------------
if have docker; then
	compose_raw=$(docker compose version --short 2>/dev/null | tr -d '\r')
	if [ -z "$compose_raw" ]; then
		fail 'docker compose plugin not available; install Docker Compose '"$DOCKER_COMPOSE_VERSION"
	else
		compose_version=${compose_raw#v}
		if [ "$compose_version" = "$DOCKER_COMPOSE_VERSION" ]; then
			ok "Docker Compose $compose_version matches the pin"
		else
			fail "Docker Compose $compose_version found but $DOCKER_COMPOSE_VERSION is pinned (design.md §16.4); install the pinned plugin version"
		fi
	fi
else
	fail 'docker not found on PATH; install Docker with the Compose v'"$DOCKER_COMPOSE_VERSION"' plugin'
fi

# --- pip-tools exact pin (lock generator, never invoked here) ----------------
PYTHON_BIN=''
for candidate in python3 python; do
	if have "$candidate"; then
		PYTHON_BIN=$candidate
		break
	fi
done

if [ -z "$PYTHON_BIN" ]; then
	fail 'python3 not found on PATH; Python 3.13 is required (design.md §16.2)'
else
	ok "Python interpreter: $($PYTHON_BIN --version 2>&1 | tr -d '\r')"
	pip_tools_raw=$("$PYTHON_BIN" -m piptools --version 2>/dev/null | tr -d '\r')
	if [ -z "$pip_tools_raw" ]; then
		fail "pip-tools==$PIP_TOOLS_VERSION not installed; run: $PYTHON_BIN -m pip install 'pip-tools==$PIP_TOOLS_VERSION'"
	else
		pip_tools_version=$(printf '%s\n' "$pip_tools_raw" | sed -n 's/.*version \([0-9][0-9.]*\).*/\1/p')
		[ -n "$pip_tools_version" ] || pip_tools_version=$(printf '%s\n' "$pip_tools_raw" | tr -cd '0-9.')
		if [ "$pip_tools_version" = "$PIP_TOOLS_VERSION" ]; then
			ok "pip-tools $pip_tools_version matches the pin"
		else
			fail "pip-tools $pip_tools_version found but ==$PIP_TOOLS_VERSION is pinned (design.md §16.2); run: $PYTHON_BIN -m pip install 'pip-tools==$PIP_TOOLS_VERSION'"
		fi
	fi
fi

# --- pre-commit framework ----------------------------------------------------
if have pre-commit; then
	ok "pre-commit present: $(pre-commit --version 2>/dev/null | tr -d '\r')"
	if [ -f .pre-commit-config.yaml ] && [ -d .git ]; then
		if pre-commit install >/dev/null 2>&1; then
			ok 'pre-commit git hooks installed'
		else
			fail 'pre-commit install failed'
		fi
	else
		ok 'pre-commit hook installation skipped (no .pre-commit-config.yaml or no .git yet)'
	fi
else
	fail 'pre-commit not found on PATH; install it with: pip install pre-commit'
fi

printf '\nbootstrap never regenerates lockfiles: use the dedicated lock targets to change them.\n'

if [ "$FAILURES" -ne 0 ]; then
	printf 'bootstrap failed with %s toolchain problem(s)\n' "$FAILURES" >&2
	exit 1
fi

printf 'bootstrap: pinned toolchain verified\n'
exit 0
