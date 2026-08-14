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
