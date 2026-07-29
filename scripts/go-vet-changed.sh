#!/bin/sh
# go vet for the packages containing the staged agent Go files (design §8.4).
#
# pre-commit passes repository-relative paths under agent/. This script maps them
# to package directories inside the agent module and vets each one once. It is
# non-mutating.
#
# Phase 0 ordering: agent/go.mod is created by task 3.1. Until it exists there is
# no module to vet, so the hook exits successfully instead of failing the commit.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ ! -f "$ROOT/agent/go.mod" ]; then
	echo 'go vet: agent/go.mod does not exist yet; nothing to vet'
	exit 0
fi

if ! command -v go >/dev/null 2>&1; then
	echo 'go vet: the go toolchain is not installed' >&2
	exit 1
fi

PKGS=''
for path in "$@"; do
	case "$path" in
	agent/*) ;;
	*) continue ;;
	esac

	dir=$(dirname -- "$path")
	# Package path relative to the agent module root.
	rel=$(printf '%s' "$dir" | sed 's|^agent/*||')
	[ -n "$rel" ] || rel='.'

	case " $PKGS " in
	*" ./$rel "*) continue ;;
	esac
	PKGS="$PKGS ./$rel"
done

if [ -z "$PKGS" ]; then
	echo 'go vet: no staged agent packages'
	exit 0
fi

cd "$ROOT/agent" || exit 1
# shellcheck disable=SC2086
exec go vet -mod=readonly $PKGS
