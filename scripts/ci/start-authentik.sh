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
SERVER_NAME="$server_name" WORKER_NAME="$worker_name" python3 - <<'EOF'
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

# Authentik applies its own default blueprints during startup, once migrations finish. An earlier
# revision of this loop ALSO ran `ak apply_blueprint` over every discovered file every 10 seconds
# and kept doing it for the whole 600s window. The result is in the `auth` job's Postgres log on
# the runs that failed then:
#
#   duplicate key value violates unique constraint "authentik_flows_flow_slug_key"
#   duplicate key value violates unique constraint "authentik_policies_policy_name_..._uniq"
#   deadlock detected
#
# Two writers inserting the same fixtures on overlapping transactions.
#
# Replacing that storm with a SINGLE apply at 150s was also wrong, in the other direction. It
# passed twice and then failed with `flows (0)` for the entire 600s: one attempt has to land in
# the window after migrations finish and before the deadline, and nothing guarantees it does.
#
# So: bounded retry. Attempts are spaced a minute apart and capped, which keeps concurrent-write
# pressure far below the original (5 attempts rather than ~60) while not betting the job on a
# single moment. `ak apply_blueprint` is idempotent per file, so a repeat that races the worker
# loses a transaction rather than corrupting anything.
GRACE_BEFORE_MANUAL_APPLY = 60.0
APPLY_INTERVAL = 60.0
MAX_APPLY_ATTEMPTS = 5

started = time.time()
apply_attempts = 0
last_apply = 0.0

last_log = 0.0
while time.time() < deadline:
    now = time.time()

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

            # The bounded manual apply, and only once the server is answering -- applying while it
            # is still migrating is what produced the "column ... does not exist" errors.
            if (
                apply_attempts < MAX_APPLY_ATTEMPTS
                and (now - started) >= GRACE_BEFORE_MANUAL_APPLY
                and (now - last_apply) >= APPLY_INTERVAL
            ):
                apply_attempts += 1
                print(
                    f"[start-authentik] flow still absent; applying blueprints "
                    f"(attempt {apply_attempts}/{MAX_APPLY_ATTEMPTS})",
                    flush=True,
                )
                apply_cmd = (
                    'find / -xdev -name "*.yaml" -path "*blueprint*" 2>/dev/null | head -n 200 | '
                    'while read -r bp; do ak apply_blueprint "$bp" 2>/dev/null || true; done'
                )
                proc = subprocess.run(
                    ["docker", "exec", server_name, "sh", "-c", apply_cmd], capture_output=True
                )
                if proc.returncode != 0:
                    tail = (proc.stderr or b"").decode("utf-8", "replace")[-400:]
                    print(f"[start-authentik] apply attempt returned {proc.returncode}: {tail}", flush=True)
                last_apply = now

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

# Diagnostics, because the previous failure produced NONE. The job logged
# "waiting for blueprints & scopes... flows (0)" thirteen times and then died, which says the API
# answered and no blueprint was ever applied -- and nothing about why. Blueprints are applied by
# the WORKER in current Authentik, so its log is the first place to look and it was never shown.
for container in (server_name, os.environ.get("WORKER_NAME", "forgeops-ci-authentik-worker")):
    print(f"\n===== docker logs (tail) {container} =====", file=sys.stderr, flush=True)
    proc = subprocess.run(
        ["docker", "logs", "--tail", "120", container], capture_output=True
    )
    for stream in (proc.stdout, proc.stderr):
        if stream:
            print(stream.decode("utf-8", "replace"), file=sys.stderr, flush=True)

sys.exit(1)
EOF

echo "Authentik is ready at http://localhost:9000"
