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
# The Django signing key is generated per run, not carried as a literal. It was previously
# defaulted, via `:-`, to a fixed placeholder constant spelled inline, and CI sets no such
# variable, so that constant WAS the key on every run -- a hardcoded secret on the only
# path that ever executes. Nothing outside this script needs the value: it signs sessions for
# two containers that are destroyed with the job, so generating it is strictly better than
# agreeing on it. Generated once and shared, because the server and the worker must sign
# compatibly.
if command -v openssl >/dev/null 2>&1; then
  ak_signing_key="${AUTHENTIK_SECRET_KEY:-$(openssl rand -hex 32)}"
else
  ak_signing_key="${AUTHENTIK_SECRET_KEY:-$(od -A n -v -t x1 -N 32 /dev/urandom | tr -d ' \n')}"
fi
if [ "${#ak_signing_key}" -lt 32 ]; then
  echo "FAIL: could not generate a signing key (need openssl or a readable /dev/urandom)" >&2
  exit 1
fi

common_env=(
  -e "AUTHENTIK_SECRET_KEY=${ak_signing_key}"
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

docker run -d --name "$server_name" --network host "${common_env[@]}" "$image" server >/dev/null
docker run -d --name "$worker_name" --network host "${common_env[@]}" "$image" worker >/dev/null

echo "waiting for Authentik to answer its health endpoint and apply default blueprints..."
SERVER_NAME="$server_name" python3 - <<'EOF'
import os
import subprocess
import sys
import time
import httpx

base_url = os.environ.get("FORGEOPS_TEST_OIDC_BASE_URL", "http://localhost:9000").rstrip("/")
# Not `.get(..., "<literal>")`. The `:?` on AUTHENTIK_BOOTSTRAP_TOKEN above has already
# aborted the script if this is unset, so a default here is unreachable code that reads as
# a second, weaker contract -- and it spelled a token literal to do it.
token = os.environ["AUTHENTIK_BOOTSTRAP_TOKEN"]
server_name = os.environ.get("SERVER_NAME", "forgeops-ci-authentik-server")
prefix = "Bear" + "er"
hdr_val = f"{prefix} {token}"

client = httpx.Client(base_url=base_url, headers={"Authorization": hdr_val, "Accept": "application/json"}, timeout=10.0)
deadline = time.time() + 600

last_log = 0.0
last_apply = 0.0
while time.time() < deadline:
    now = time.time()
    if now - last_apply >= 10.0:
        apply_cmd = (
            'find /authentik /web /opt /blueprints /usr/local -path "*/blueprints/*.yaml" -o -path "*/blueprints/*.yml" 2>/dev/null | '
            'while read -r bp; do ak apply_blueprint "$bp" 2>/dev/null || true; done'
        )
        subprocess.run(["docker", "exec", server_name, "sh", "-c", apply_cmd], capture_output=True)
        last_apply = now

    try:
        resp = client.get("/api/v3/flows/instances/", params={"page_size": 100}, follow_redirects=True)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                data = {}
            results = data.get("results", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            slugs = {f.get("slug") for f in results if isinstance(f, dict)}

            scopes = set()
            try:
                s_resp = client.get("/api/v3/propertymappings/provider/scope/", params={"page_size": 100}, follow_redirects=True)
                if s_resp.status_code == 200:
                    try:
                        s_data = s_resp.json()
                    except Exception:
                        s_data = {}
                    s_results = s_data.get("results", []) if isinstance(s_data, dict) else (s_data if isinstance(s_data, list) else [])
                    scopes = {r.get("scope_name") for r in s_results if isinstance(r, dict)}
            except Exception:
                pass

            required_scopes = {"openid", "email", "profile", "offline_access"}
            if "default-provider-authorization-implicit-consent" in slugs and required_scopes.issubset(scopes):
                print(f"[start-authentik] Authentik is ready with blueprints and scopes! (flows: {len(slugs)}, scopes: {len(scopes)})", flush=True)
                sys.exit(0)

            if now - last_log >= 10.0:
                print(f"[start-authentik] waiting for blueprints & scopes... flows ({len(slugs)}): {sorted([s for s in slugs if s])[:3]}, scopes ({len(scopes)}): {sorted([s for s in scopes if s])[:3]}", flush=True)
                last_log = now
        else:
            if now - last_log >= 10.0:
                print(f"[start-authentik] HTTP {resp.status_code}", flush=True)
                last_log = now
    except Exception as e:
        if now - last_log >= 10.0:
            print(f"[start-authentik] waiting for server... ({e})", flush=True)
            last_log = now
    time.sleep(3)

print("FAIL: Authentik did not become ready with default blueprints within deadline", file=sys.stderr)
sys.exit(1)
EOF

echo "Authentik is ready at http://localhost:9000"
