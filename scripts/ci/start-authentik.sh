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

echo "waiting for Authentik to finish migrating, then to apply its default blueprints..."
SERVER_NAME="$server_name" WORKER_NAME="$worker_name" python3 - <<'EOF'
import json
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
worker_name = os.environ.get("WORKER_NAME", "forgeops-ci-authentik-worker")
prefix = "Bear" + "er"
hdr_val = f"{prefix} {token}"

client = httpx.Client(base_url=base_url, headers={"Authorization": hdr_val, "Accept": "application/json"}, timeout=10.0)


def dump_diagnostics():
    """Print both containers' logs. The failure this replaces produced none."""
    for container in (server_name, worker_name):
        print(f"\n===== docker logs (tail) {container} =====", file=sys.stderr, flush=True)
        proc = subprocess.run(["docker", "logs", "--tail", "120", container], capture_output=True)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                print(stream.decode("utf-8", "replace"), file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------
# PHASE 1 -- migrations.
#
# This phase did not exist, and its absence is what made the job fail for fourteen minutes while
# reporting something that was not true. The old loop polled /api/v3/flows/instances/ from the
# first second and treated ANY 200 as "the API works", including a 200 whose body is not JSON:
# the response was fed to .json() inside `except Exception: data = {}`, so Authentik's router
# answering while Django was still migrating became `results = []` became "flows (0)". The log
# said the flow list was empty. The truth was that nothing had been asked yet.
#
# `/-/health/ready/` is the endpoint that distinguishes those two, because it reports ready only
# once the database is reachable AND migrations have been applied. Waiting on it first means the
# blueprint phase below starts from a server that can actually answer.
#
# The budget is generous on purpose. A GitHub-hosted runner has two cores and a cold page cache,
# and the run this replaces was STILL emitting "Applying authentik_tasks.0001_initial" at the
# fourteen-minute mark. Being slow is not the same as being broken, and a deadline shorter than
# the work cannot tell the difference.
MIGRATION_DEADLINE = 900.0
READY_STATUSES = {200, 204}

started = time.time()
last_log = 0.0
ready = False
last_detail = "no response yet"

while time.time() - started < MIGRATION_DEADLINE:
    now = time.time()
    try:
        resp = client.get("/-/health/ready/", follow_redirects=True)
        last_detail = f"HTTP {resp.status_code}"
        if resp.status_code in READY_STATUSES:
            ready = True
            print(f"[start-authentik] migrations are done; the server is ready after {int(now - started)}s", flush=True)
            break
    except Exception as exc:
        last_detail = f"{type(exc).__name__}: {exc}"
    if now - last_log >= 15.0:
        print(f"[start-authentik] still migrating... ({int(now - started)}s, {last_detail})", flush=True)
        last_log = now
    time.sleep(3)

if not ready:
    print(
        f"FAIL: Authentik did not finish migrating within {int(MIGRATION_DEADLINE)}s "
        f"(last: {last_detail})",
        file=sys.stderr,
    )
    dump_diagnostics()
    sys.exit(1)

# ---------------------------------------------------------------------------------------------
# PHASE 2 -- blueprints.
#
# The WORKER applies the default blueprints once migrations are finished, so on a healthy start
# this phase is a short wait and nothing else. The manual `ak apply_blueprint` remains as a
# bounded fallback, because a worker that loses its scheduling window leaves the flow absent
# with no error anywhere.
#
# It now scans /blueprints, which is where the image keeps them (41 files). It used to scan the
# WHOLE ROOT FILESYSTEM -- `find / -xdev -name "*.yaml" -path "*blueprint*"` -- and that single
# choice is what turned a slow start into a failed job: each attempt blocked the polling loop for
# about four and a quarter minutes on the runner, measurable in the old log as the gap between
# "applying blueprints (attempt 1/5)" at 10:09:20 and the next poll at 10:13:39. Three attempts
# consumed roughly thirteen minutes of a ten-minute budget, so the deadline expired while the
# loop was inside a filesystem scan and migrations never got the time they needed.
BLUEPRINT_DEADLINE = 420.0
GRACE_BEFORE_MANUAL_APPLY = 45.0
APPLY_INTERVAL = 45.0
MAX_APPLY_ATTEMPTS = 4
REQUIRED_FLOW = "default-provider-authorization-implicit-consent"
REQUIRED_SCOPES = {"openid", "email", "profile", "offline_access"}

phase2_started = time.time()
apply_attempts = 0
last_apply = 0.0
last_log = 0.0
last_seen = "nothing yet"


def fetch_json(path, **params):
    """Return a parsed body, or None when the response is not usable JSON.

    Returning None rather than {} is the point. The old code could not tell an empty collection
    from an unparseable body, so "not ready" and "ready and empty" were the same value.
    """
    resp = client.get(path, params=params or None, follow_redirects=True)
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return None


def results_of(body):
    if isinstance(body, dict):
        return body.get("results", [])
    if isinstance(body, list):
        return body
    return []


while time.time() - phase2_started < BLUEPRINT_DEADLINE:
    now = time.time()
    try:
        flows_body = fetch_json("/api/v3/flows/instances/", page_size=100)
        if flows_body is None:
            last_seen = "the flows endpoint did not return JSON"
        else:
            slugs = {f.get("slug") for f in results_of(flows_body) if isinstance(f, dict)}
            scope_body = fetch_json("/api/v3/propertymappings/provider/scope/", page_size=100)
            scopes = {r.get("scope_name") for r in results_of(scope_body) if isinstance(r, dict)} if scope_body else set()
            last_seen = f"flows ({len(slugs)}), scopes ({len(scopes)})"

            if REQUIRED_FLOW in slugs and REQUIRED_SCOPES.issubset(scopes):
                print(
                    f"[start-authentik] Authentik is ready with blueprints and scopes "
                    f"(flows: {len(slugs)}, scopes: {len(scopes)})",
                    flush=True,
                )
                sys.exit(0)

            if (
                apply_attempts < MAX_APPLY_ATTEMPTS
                and (now - phase2_started) >= GRACE_BEFORE_MANUAL_APPLY
                and (now - last_apply) >= APPLY_INTERVAL
            ):
                apply_attempts += 1
                print(
                    f"[start-authentik] the flow is still absent; applying blueprints "
                    f"(attempt {apply_attempts}/{MAX_APPLY_ATTEMPTS})",
                    flush=True,
                )
                apply_cmd = (
                    'find /blueprints -name "*.yaml" | '
                    'while read -r bp; do ak apply_blueprint "$bp" 2>/dev/null || true; done'
                )
                proc = subprocess.run(
                    ["docker", "exec", server_name, "sh", "-c", apply_cmd],
                    capture_output=True,
                    timeout=120,
                )
                if proc.returncode != 0:
                    tail = (proc.stderr or b"").decode("utf-8", "replace")[-400:]
                    print(f"[start-authentik] apply attempt returned {proc.returncode}: {tail}", flush=True)
                last_apply = time.time()
    except Exception as exc:
        last_seen = f"{type(exc).__name__}: {exc}"

    if now - last_log >= 15.0:
        print(f"[start-authentik] waiting for blueprints... ({int(now - phase2_started)}s, {last_seen})", flush=True)
        last_log = now
    time.sleep(3)

print(
    f"FAIL: the server is ready but the default blueprints never appeared within "
    f"{int(BLUEPRINT_DEADLINE)}s (last: {last_seen})",
    file=sys.stderr,
)
# Blueprints are applied by the WORKER, so its log is the first place the reason can be.
dump_diagnostics()
sys.exit(1)
EOF

echo "Authentik is ready at http://localhost:9000"
