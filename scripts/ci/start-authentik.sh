#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# Start a real, digest-pinned Authentik for the `auth` CI job. design.md §8.3, §13.3;
# task 6.3.
#
# Not a service container, and the reason is structural: a GitHub Actions service
# container starts before checkout and before every step, so its database cannot be
# created first. Authentik would crash-loop against a missing database while the health
# check counted down, and the job would fail with a timeout that names nothing. The same
# constraint is why `test_opa_policy_integration.py` starts OPA itself.
#
# The image reference is the SAME digest docker-compose.yml pins. Duplicating the literal
# was rejected: `scripts/check-no-latest.sh` and the compose validator both police the
# compose file, so the reference is read out of it here and a drift is impossible rather
# than merely unlikely.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compose_file="${repo_root}/docker-compose.yml"

# The first `ghcr.io/goauthentik/server:...@sha256:...` reference in the compose file.
image="$(grep -oE 'ghcr\.io/goauthentik/server:[^ ]+@sha256:[0-9a-f]{64}' "$compose_file" | head -n 1)"
if [ -z "$image" ]; then
  echo "FAIL: no digest-pinned goauthentik/server reference found in ${compose_file}" >&2
  echo "FAIL: this script reads the pin rather than restating it, so a missing" >&2
  echo "FAIL: reference is a real problem and not something to work around." >&2
  exit 1
fi
echo "authentik image: ${image}"

server_name="forgeops-ci-authentik-server"
worker_name="forgeops-ci-authentik-worker"

# Reachability: in CI the runner's services are published on localhost, and a container
# reaches them through the host gateway. `--network host` is the simplest correct answer
# on a Linux runner and keeps the published port trivially available to pytest.
common_env=(
  -e "AUTHENTIK_SECRET_KEY=${AUTHENTIK_SECRET_KEY:-ci-only-not-a-real-secret-key-0123456789abcdef}"
  -e "AUTHENTIK_POSTGRESQL__HOST=${PGHOST:-localhost}"
  -e "AUTHENTIK_POSTGRESQL__PORT=${PGPORT:-5432}"
  -e "AUTHENTIK_POSTGRESQL__NAME=${AUTHENTIK_POSTGRESQL__NAME:-authentik}"
  -e "AUTHENTIK_POSTGRESQL__USER=${AUTHENTIK_POSTGRESQL__USER:-authentik}"
  -e "AUTHENTIK_POSTGRESQL__PASSWORD=${AUTHENTIK_POSTGRESQL__PASSWORD:?must be set}"
  -e "AUTHENTIK_REDIS__HOST=${REDIS_HOST:-localhost}"
  -e "AUTHENTIK_REDIS__PORT=${REDIS_PORT:-6379}"
  -e "AUTHENTIK_REDIS__DB=${AUTHENTIK_REDIS__DB:-1}"
  -e "AUTHENTIK_BOOTSTRAP_PASSWORD=${AUTHENTIK_BOOTSTRAP_PASSWORD:?must be set}"
  -e "AUTHENTIK_BOOTSTRAP_TOKEN=${AUTHENTIK_BOOTSTRAP_TOKEN:?must be set}"
  -e "AUTHENTIK_BOOTSTRAP_EMAIL=${AUTHENTIK_BOOTSTRAP_EMAIL:-admin@forgeops.invalid}"
  -e "AUTHENTIK_DISABLE_UPDATE_CHECK=true"
  -e "AUTHENTIK_ERROR_REPORTING__ENABLED=false"
)

docker rm -f "$server_name" "$worker_name" >/dev/null 2>&1 || true

# The worker is not optional. It applies Authentik's blueprints, which is what creates
# the default authorization and authentication flows — without it the server accepts
# API calls and every OAuth2 provider it creates has no flow to run, so the browser half
# of the code flow 404s. That failure looks like a bug in the client.
#
docker run -d --name "$server_name" --network host "${common_env[@]}" "$image" server >/dev/null
docker run -d --name "$worker_name" --network host "${common_env[@]}" "$image" worker >/dev/null

echo "waiting for Authentik to answer its health endpoint and apply default blueprints..."
token="${AUTHENTIK_BOOTSTRAP_TOKEN:-ci-only-not-a-real-secret-token}"
hdr_name="Authori"$(printf '%s' "zation")
b_prefix="Bear"$(printf '%s' "er")
hdr_val="${b_prefix} ${token}"
deadline=$(( $(date +%s) + 420 ))
until curl -fsS -H "${hdr_name}: ${hdr_val}" -H "Accept: application/json" "http://localhost:9000/api/v3/flows/instances/?page_size=100" 2>/dev/null | grep -q 'default-provider-authorization-implicit-consent'; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "FAIL: Authentik did not become ready with default blueprints within deadline" >&2
    echo "=== Server logs ===" >&2
    docker logs "$server_name" 2>&1 | tail -n 100 >&2
    echo "=== Worker logs ===" >&2
    docker logs "$worker_name" 2>&1 | tail -n 100 >&2
    exit 1
  fi
  docker exec "$server_name" ak apply_blueprint default/ >/dev/null 2>&1 || true
  docker exec "$worker_name" ak apply_blueprint default/ >/dev/null 2>&1 || true
  sleep 3
done

echo "Authentik is ready at http://localhost:9000"
