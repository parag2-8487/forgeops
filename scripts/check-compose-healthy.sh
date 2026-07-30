#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
# scripts/check-compose-healthy.sh — assert every default-profile service is HEALTHY,
# not merely running (design.md §0.5 debt D2, §2.3, §8.3).
#
# Why this is not just `docker compose up --wait`:
# `--wait` treats a service with no healthcheck as satisfied once it is *running*. A
# Next.js server that starts and then fails to serve, or a backend that boots and then
# cannot reach Postgres, both look like success. This script requires an explicit
# `healthy` status for every service in the committed default set, so a missing
# healthcheck is a build failure rather than a silent exemption.
#
# Failure is exit 1 naming each service and its observed status.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXPECTED_FILE="$SCRIPT_DIR/compose-default-services.txt"

cd "$REPO_ROOT"

if [ ! -f "$EXPECTED_FILE" ]; then
	printf 'ERROR: missing %s\n' "$EXPECTED_FILE" >&2
	exit 1
fi

# shellcheck disable=SC2312
mapfile -t EXPECTED < <(grep -v '^#' "$EXPECTED_FILE" | grep -v '^[[:space:]]*$')

if [ "${#EXPECTED[@]}" -eq 0 ]; then
	printf 'ERROR: the expected service set is empty; the check would pass vacuously\n' >&2
	exit 1
fi

printf '==> check-compose-healthy: %d expected service(s)\n' "${#EXPECTED[@]}"

FAILED=0
for service in "${EXPECTED[@]}"; do
	container="$(docker compose ps -q "$service" 2>/dev/null || true)"
	if [ -z "$container" ]; then
		printf 'ERROR: %-18s no container (service not started)\n' "$service" >&2
		FAILED=1
		continue
	fi

	# A container with no healthcheck reports an empty .State.Health.Status. That is
	# treated as a failure on purpose: "we cannot tell" must not read as "healthy".
	status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container")"
	running="$(docker inspect -f '{{.State.Running}}' "$container")"

	case "$status" in
	healthy)
		printf 'ok:   %-18s healthy\n' "$service"
		;;
	no-healthcheck)
		printf 'ERROR: %-18s has NO healthcheck (running=%s); add one so `--wait` means something\n' \
			"$service" "$running" >&2
		FAILED=1
		;;
	*)
		printf 'ERROR: %-18s status=%s running=%s\n' "$service" "$status" "$running" >&2
		docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$container" >&2 || true
		FAILED=1
		;;
	esac
done

if [ "$FAILED" -ne 0 ]; then
	printf '\ncheck-compose-healthy: FAILED\n' >&2
	exit 1
fi

printf 'check-compose-healthy: every default service is healthy\n'
