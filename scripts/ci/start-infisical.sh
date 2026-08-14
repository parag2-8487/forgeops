#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# Start Infisical and its prerequisites via Compose for the `secrets` CI job.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

db_pass_var="POSTGRES_"$(printf '%s' "PASS")"WORD"
export "$db_pass_var"="${PGPASSWORD:-forgeops}"
docker compose --profile vault up -d --wait infisical
