#!/bin/sh
# SPDX-License-Identifier: FSL-1.1-ALv2
# check-tofu-lock.sh — verify the committed six-platform null-provider lock.
#
# Two checks, per design.md §10.6 and task 15.6:
#   1. `tofu init -lockfile=readonly` succeeds against the committed lock, so the
#      lock is sufficient to install the provider without being rewritten.
#   2. Regenerating the six-platform lock in an ISOLATED COPY produces no diff,
#      so the committed lock genuinely covers all six supported targets.
#
# The fixture directory is never mutated: all work happens in a temp copy.
set -eu

FIXTURE="agent/testfixtures/tofu-null"
PLATFORMS="-platform=linux_amd64 -platform=linux_arm64 -platform=darwin_amd64 -platform=darwin_arm64 -platform=windows_amd64 -platform=windows_arm64"

if [ ! -f "$FIXTURE/main.tf" ]; then
  printf 'check-tofu-lock: FAIL missing fixture %s/main.tf\n' "$FIXTURE" >&2
  exit 1
fi
if [ ! -f "$FIXTURE/.terraform.lock.hcl" ]; then
  printf 'check-tofu-lock: FAIL missing committed lock %s/.terraform.lock.hcl\n' "$FIXTURE" >&2
  exit 1
fi

if ! command -v tofu >/dev/null 2>&1; then
  printf 'check-tofu-lock: SKIP tofu not on PATH (install OpenTofu 1.12.5)\n'
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

cp "$FIXTURE/main.tf" "$WORK/main.tf"
cp "$FIXTURE/.terraform.lock.hcl" "$WORK/.terraform.lock.hcl"

printf 'check-tofu-lock: tofu init -lockfile=readonly\n'
( cd "$WORK" && tofu init -lockfile=readonly -input=false >/dev/null )

printf 'check-tofu-lock: regenerating six-platform lock in an isolated copy\n'
# shellcheck disable=SC2086
( cd "$WORK" && tofu providers lock $PLATFORMS >/dev/null )

if ! diff -u "$FIXTURE/.terraform.lock.hcl" "$WORK/.terraform.lock.hcl" >/dev/null; then
  printf 'check-tofu-lock: FAIL committed lock drifted from the six-platform regeneration\n' >&2
  diff -u "$FIXTURE/.terraform.lock.hcl" "$WORK/.terraform.lock.hcl" >&2 || true
  exit 1
fi

printf 'check-tofu-lock: OK six-platform lock is fresh and readonly-init succeeds\n'
