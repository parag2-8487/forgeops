#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/lock-backend.sh — Regenerate hash-pinned backend lockfiles from pyproject.toml.
# Uses pip-tools==7.6.0. Both lockfiles are committed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

# shellcheck source=scripts/lib/pip-compile.sh
. "$SCRIPT_DIR/lib/pip-compile.sh"
resolve_pip_compile "$REPO_ROOT"

cd "$BACKEND_DIR"

printf '==> lock-backend: generating requirements.lock (runtime only)\n'
run_pip_compile \
  --generate-hashes \
  --allow-unsafe \
  --strip-extras \
  --output-file=requirements.lock \
  --quiet \
  pyproject.toml

printf '==> lock-backend: generating requirements-dev.lock (runtime + dev)\n'
run_pip_compile \
  --generate-hashes \
  --allow-unsafe \
  --strip-extras \
  --extra=dev \
  --output-file=requirements-dev.lock \
  --quiet \
  pyproject.toml

printf 'lock-backend: both lockfiles regenerated successfully\n'
