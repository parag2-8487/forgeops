#!/usr/bin/env bash
# scripts/check-compose.sh — Static Compose assertion for the Phase 0 data plane.
# Validates docker-compose.yml without Docker; parses YAML with Python/PyYAML.
# Design: §2.2, §13.3, §14.2, §16.4
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.yml"

if [ ! -f "${COMPOSE_FILE}" ]; then
  echo "FAIL: docker-compose.yml not found at ${COMPOSE_FILE}" >&2
  exit 1
fi

# Run the Python validator
python "${REPO_ROOT}/scripts/check-compose-validate.py" "${COMPOSE_FILE}"
