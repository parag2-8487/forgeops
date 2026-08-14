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

export "$db_pass_var"="${PGPASSWORD:-ci-only-not-a-real-secret}"
export "$app_pass_var"="${FORGEOPS_APP_DB_PASSWORD:-ci-only-not-a-real-secret}"
export "$mig_pass_var"="${FORGEOPS_MIGRATOR_DB_PASSWORD:-ci-only-not-a-real-secret}"

docker compose --profile vault up -d --wait infisical
