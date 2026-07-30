#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# Start a real, digest-pinned Cerbos sidecar for the `backend` and `auth` CI jobs.
# design.md §2.3, §11.2, §13.3, D-55; task 6.4.
#
# Not a service container, and the reason is the same one that keeps OPA and Authentik
# out of the `services:` block: a service container starts before checkout, so it cannot
# mount `./policies/cerbos` or `./config/cerbos`. A Cerbos with no policy repository
# starts, answers its health endpoint, and denies everything — which is the worst
# possible failure, because the RBAC matrix's deny rows would all pass and its allow rows
# would fail with "Cerbos said False", reading like a policy bug rather than a missing
# mount.
#
# The image reference is READ OUT of docker-compose.yml rather than restated, for the
# same reason `start-authentik.sh` does it: the compose file is what the pin checks
# police, so drift between CI and Compose becomes impossible rather than merely unlikely.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compose_file="${repo_root}/docker-compose.yml"

image="$(grep -oE 'ghcr\.io/cerbos/cerbos:[^ ]+@sha256:[0-9a-f]{64}' "$compose_file" | head -n 1)"
if [ -z "$image" ]; then
  echo "FAIL: no digest-pinned ghcr.io/cerbos/cerbos reference found in ${compose_file}" >&2
  echo "FAIL: this script reads the pin rather than restating it, so a missing" >&2
  echo "FAIL: reference is a real problem and not something to work around." >&2
  exit 1
fi
echo "cerbos image: ${image}"

name="forgeops-ci-cerbos"
port="${FORGEOPS_CI_CERBOS_PORT:-3592}"

docker rm -f "$name" >/dev/null 2>&1 || true

# The policy set is validated BEFORE the server starts, with Cerbos's own test runner.
# `compile` executes policies/cerbos/matrix_test.yaml, so a policy that no longer matches
# §11.2's table fails here — at the pinned version, with no Python in the loop — instead
# of surfacing as a confusing integration failure later in the job.
docker run --rm -v "${repo_root}/policies/cerbos:/policies:ro" "$image" compile /policies

docker run -d --name "$name" \
  -p "127.0.0.1:${port}:3592" \
  -v "${repo_root}/policies/cerbos:/policies:ro" \
  -v "${repo_root}/config/cerbos:/config:ro" \
  "$image" server --config=/config/cerbos.yaml >/dev/null

echo "waiting for Cerbos to answer its own health endpoint..."
deadline=$(( $(date +%s) + 90 ))
until curl -fsS -o /dev/null "http://127.0.0.1:${port}/_cerbos/health"; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "FAIL: Cerbos did not become ready within 90s" >&2
    docker logs "$name" 2>&1 | tail -n 100 >&2
    exit 1
  fi
  sleep 2
done

# A serving Cerbos with an empty policy repository would pass the health check above and
# then deny every allow row in the matrix. Asserting the policy count here turns that into
# one clear failure instead of forty confusing ones.
policies="$(docker logs "$name" 2>&1 | grep -oE 'Found [0-9]+ executable policies' | head -n 1 || true)"
echo "${policies:-no policy-count line in the log}"
if [ -z "$policies" ] || [ "$policies" = "Found 0 executable policies" ]; then
  echo "FAIL: Cerbos loaded no executable policies; the /policies mount is wrong" >&2
  docker logs "$name" 2>&1 | tail -n 50 >&2
  exit 1
fi

echo "Cerbos is ready at http://127.0.0.1:${port}"
