#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/lib/pip-compile.sh — resolve the pinned pip-compile entry point.
#
# Sourced by scripts/lock-backend.sh and scripts/check-lock-freshness.sh.
#
# design.md §7.7 and §16.2 make `pip-tools==7.6.0` the ONLY lock generator, so
# this resolver refuses any other version rather than silently producing a lock
# with different resolution behaviour. `pip-compile` is frequently not on PATH
# (it lives inside the backend virtualenv), so the venv is searched first and the
# module entry point `python -m piptools compile` is used, which works on both
# Windows (Scripts/) and POSIX (bin/) layouts.
#
# ── Why the lock is generated on LINUX regardless of the developer's OS ───────
#
# pip-compile output is PLATFORM-DEPENDENT: environment markers resolve against
# the interpreter doing the resolution, so a Windows run and a Linux run produce
# genuinely different pinned sets. The committed lock is consumed by exactly two
# Linux things — the backend runtime image and CI — so Linux is the only correct
# generation target. A Windows-generated lock passes locally and then fails CI
# with "requirements.lock is stale", which is precisely what happened before this
# indirection existed.
#
# So: on Linux the local interpreter is used directly; on any other OS the
# resolution runs inside a digest-pinned python:3.13-slim container. Both paths
# install the same pinned pip-tools, so both produce byte-identical output.
# Set FORGEOPS_LOCK_NATIVE=1 to force the local interpreter (useful only for
# debugging — it will produce a lock CI rejects on a non-Linux host).

PIP_TOOLS_REQUIRED_VERSION="${FORGEOPS_PIP_TOOLS_VERSION:-7.6.0}"

# Digest-pinned per design §7.7: a tag is mutable, a digest is not, and the whole
# point here is a reproducible resolution.
PIP_COMPILE_IMAGE="${FORGEOPS_LOCK_IMAGE:-python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91}"

_PIP_COMPILE_PYTHON=""
_PIP_COMPILE_MODE=""
_PIP_COMPILE_ROOT=""

_pip_compile_host_is_linux() {
	[ "$(uname -s 2>/dev/null || echo unknown)" = "Linux" ]
}

_pip_compile_probe_python() {
	# _pip_compile_probe_python <python executable>
	[ -n "${1:-}" ] || return 1
	command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || return 1
	"$1" -c 'import piptools' >/dev/null 2>&1 || return 1
	# `python -m piptools` has no --version flag (only the pip-compile console
	# script does), so the installed distribution metadata is authoritative.
	_version=$("$1" -c 'import importlib.metadata as m; print(m.version("pip-tools"))' 2>/dev/null | tr -d '\r')
	[ -n "$_version" ] || return 1
	if [ "$_version" != "$PIP_TOOLS_REQUIRED_VERSION" ]; then
		printf 'note: %s has pip-tools %s, but ==%s is pinned; ignoring it\n' \
			"$1" "$_version" "$PIP_TOOLS_REQUIRED_VERSION" >&2
		return 1
	fi
	_PIP_COMPILE_PYTHON="$1"
	return 0
}

resolve_pip_compile() {
	# resolve_pip_compile <repo root>
	_root="${1:?resolve_pip_compile needs the repository root}"
	_PIP_COMPILE_ROOT="$_root"

	# Container path: correct on every non-Linux host, and the only way a Windows
	# developer can produce a lock that CI will accept.
	if [ -z "${FORGEOPS_LOCK_NATIVE:-}" ] && ! _pip_compile_host_is_linux; then
		if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
			_PIP_COMPILE_MODE="docker"
			printf 'using pip-tools %s in %s (host is not Linux; lock must be Linux-resolved)\n' \
				"$PIP_TOOLS_REQUIRED_VERSION" "${PIP_COMPILE_IMAGE%%@*}"
			return 0
		fi
		printf 'ERROR: this host is not Linux, so the lock must be resolved in a container,\n' >&2
		printf '       but Docker is unavailable. Start Docker Desktop and retry.\n' >&2
		printf '       (FORGEOPS_LOCK_NATIVE=1 forces local resolution, but the resulting\n' >&2
		printf '        lock will be rejected by CI as stale.)\n' >&2
		return 1
	fi

	for _candidate in \
		"${FORGEOPS_PYTHON:-}" \
		"$_root/backend/.venv/Scripts/python.exe" \
		"$_root/backend/.venv/bin/python" \
		"$_root/.venv/Scripts/python.exe" \
		"$_root/.venv/bin/python" \
		python3 \
		python; do
		if _pip_compile_probe_python "$_candidate"; then
			_PIP_COMPILE_MODE="native"
			printf 'using pip-tools %s via %s\n' "$PIP_TOOLS_REQUIRED_VERSION" "$_PIP_COMPILE_PYTHON"
			return 0
		fi
	done

	printf 'ERROR: pip-tools==%s not found.\n' "$PIP_TOOLS_REQUIRED_VERSION" >&2
	printf '       Create the backend virtualenv and install the pinned generator:\n' >&2
	printf '         py -3.13 -m venv backend/.venv\n' >&2
	printf "         backend/.venv/Scripts/python -m pip install 'pip-tools==%s'\n" \
		"$PIP_TOOLS_REQUIRED_VERSION" >&2
	return 1
}

run_pip_compile() {
	# run_pip_compile <pip-compile arguments...>
	#
	# Arguments are passed through unchanged. In container mode the caller's
	# working directory is bind-mounted at /w, so relative paths (which is all the
	# callers use) resolve identically in both modes.
	case "$_PIP_COMPILE_MODE" in
	native)
		"$_PIP_COMPILE_PYTHON" -m piptools compile "$@"
		;;
	docker)
		# Two Git-Bash/MSYS hazards to defuse here:
		#  1. pwd reports `/c/...`, which the Docker CLI rejects as non-absolute, so
		#     the mount source is converted with cygpath (falling back to pwd on
		#     shells that have no cygpath: WSL, macOS, Linux).
		#  2. MSYS rewrites arguments that look like absolute POSIX paths, turning
		#     `-w /w` into `-w W:/`. MSYS_NO_PATHCONV / MSYS2_ARG_CONV_EXCL stop it
		#     mangling the container-side paths.
		_mount="$(cygpath -w "$(pwd)" 2>/dev/null || pwd)"
		MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm \
			-v "$_mount:/w" \
			-w /w \
			"$PIP_COMPILE_IMAGE" \
			sh -c "pip install -q 'pip-tools==$PIP_TOOLS_REQUIRED_VERSION' && exec pip-compile \"\$@\"" \
			-- "$@"
		;;
	*)
		printf 'ERROR: run_pip_compile called before resolve_pip_compile succeeded\n' >&2
		return 1
		;;
	esac
}
