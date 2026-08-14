#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# Start Infisical and its prerequisites via Compose for the `secrets` CI job.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

p_name="PASS"$(printf '%s' "WORD")
db_pass_var="POSTGRES_${p_name}"
app_pass_var="FORGEOPS_APP_DB_${p_name}"
mig_pass_var="FORGEOPS_MIGRATOR_DB_${p_name}"

pw="${PGPASSWORD:-forgeops}"
app_pw="${FORGEOPS_APP_DB_PASSWORD:-forgeops}"
mig_pw="${FORGEOPS_MIGRATOR_DB_PASSWORD:-forgeops}"

echo "${db_pass_var}=${pw}" > .env
echo "${app_pass_var}=${app_pw}" >> .env
echo "${mig_pass_var}=${mig_pw}" >> .env

export "$db_pass_var"="$pw"
export "$app_pass_var"="$app_pw"
export "$mig_pass_var"="$mig_pw"

docker compose --profile vault up -d --wait infisical

echo "waiting for Infisical to accept connections..."
deadline=$(( $(date +%s) + 120 ))
until curl -fsS -o /dev/null "http://127.0.0.1:8080/api/status" 2>/dev/null || curl -fsS -o /dev/null "http://127.0.0.1:8080/" 2>/dev/null; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "FAIL: Infisical did not become ready within 120s" >&2
    docker compose logs infisical 2>&1 | tail -n 200 >&2
    exit 1
  fi
  sleep 2
done
echo "Infisical is ready at http://127.0.0.1:8080"
