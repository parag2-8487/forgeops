#!/bin/sh
# Phase 0 environment initialisation (design §2.2, §13.1, §13.3, §13.4).
#
# Copies the committed `.env.example` baseline to `.env` ONLY when `.env` is
# absent. `.env` is an optional local override: Compose already loads
# `.env.example` as required and `.env` as optional, so this script never has to
# produce `.env` for a fresh clone to start.
#
# Guarantees (design §13.3, completion criterion 4):
#   * an existing `.env` is left byte-identical — never truncated, merged or
#     overwritten, so repeated runs are idempotent;
#   * creation uses POSIX noclobber, so a concurrent creator is treated as
#     success rather than an error;
#   * exit status is 0 for "created" and "already present", non-zero only for a
#     real failure (missing baseline, unwritable directory).

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

BASELINE=.env.example
TARGET=.env

if [ ! -f "$BASELINE" ]; then
	printf 'init-env: FAIL: committed baseline %s is missing\n' "$BASELINE" >&2
	exit 1
fi

if [ -e "$TARGET" ]; then
	printf 'init-env: %s already exists; leaving it unchanged\n' "$TARGET"
	exit 0
fi

# `set -C` (noclobber) makes the redirection fail instead of truncating when the
# file appeared between the check above and this line.
if (set -C; : >"$TARGET") 2>/dev/null; then
	if cat "$BASELINE" >"$TARGET"; then
		printf 'init-env: created %s from %s\n' "$TARGET" "$BASELINE"
		exit 0
	fi
	printf 'init-env: FAIL: could not copy %s to %s\n' "$BASELINE" "$TARGET" >&2
	rm -f "$TARGET"
	exit 1
fi

if [ -e "$TARGET" ]; then
	printf 'init-env: %s was created concurrently; nothing to do\n' "$TARGET"
	exit 0
fi

printf 'init-env: FAIL: could not create %s\n' "$TARGET" >&2
exit 1
