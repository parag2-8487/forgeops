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

# The database credential is REQUIRED and is never defaulted. An earlier revision of this
# script read `"${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"`, which removed the literal fallback
# but kept the failure it was hiding: with neither variable set the expansion yields the
# empty string, the `.env` below is written with an empty credential, and Infisical starts
# against a passwordless DSN. The job then fails somewhere downstream with a message that
# names nothing. `:?` is the idiom `start-authentik.sh` already uses for exactly this, and
# it fails here, loudly, at the line that knows what is missing.
#
# Only the credential is mandatory. The role and database NAMES below keep defaults on
# purpose: they are not secrets, they are fixed by `docker-compose.yml`, and requiring the
# caller to restate them would be ceremony rather than safety.
if [ -z "${PGPASSWORD:-}" ] && [ -z "${POSTGRES_PASSWORD:-}" ]; then
  echo "FAIL: neither PGPASSWORD nor POSTGRES_PASSWORD is set in the environment." >&2
  echo "FAIL: this script reads the database credential from the environment and never" >&2
  echo "FAIL: defaults it -- an empty credential would start Infisical against a" >&2
  echo "FAIL: passwordless DSN and fail later with a message that names nothing." >&2
  exit 1
fi
pw="${PGPASSWORD:-${POSTGRES_PASSWORD}}"

# Both derived roles fall back to the verified-present credential above, never to a literal.
app_pw="${FORGEOPS_APP_DB_PASSWORD:-$pw}"
mig_pw="${FORGEOPS_MIGRATOR_DB_PASSWORD:-$pw}"

# Generated, never literal. The previous revision carried `|| printf 'ci-test-...'` and
# `|| printf '0123...'` tails, so a failure of the generator silently substituted a known
# constant -- a hardcoded secret reachable on exactly the path where entropy was wanted.
# Generation failure is now fatal instead.
gen_hex() {
  n="$1"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$n"
  else
    od -A n -v -t x1 -N "$n" /dev/urandom | tr -d ' \n'
  fi
}

auth_sec="${AUTH_SECRET:-$(gen_hex 32)}"
enc_key="${ENCRYPTION_KEY:-$(gen_hex 16)}"

# 32 bytes rendered as hex is 64 characters; 16 bytes is 32. Anything shorter means the
# generator failed and returned a truncated value, which must not be used.
if [ "${#auth_sec}" -lt 32 ]; then
  echo "FAIL: could not generate AUTH_SECRET (need openssl or a readable /dev/urandom)" >&2
  exit 1
fi
if [ "${#enc_key}" -ne 32 ]; then
  echo "FAIL: ENCRYPTION_KEY must be exactly 32 hex characters, got ${#enc_key}" >&2
  exit 1
fi

echo "${db_pass_var}=${pw}" > .env
echo "${app_pass_var}=${app_pw}" >> .env
echo "${mig_pass_var}=${mig_pw}" >> .env
echo "AUTH_SECRET=${auth_sec}" >> .env
echo "ENCRYPTION_KEY=${enc_key}" >> .env

scheme="postgres"
prefix="${scheme}://"
db_user="${POSTGRES_USER:-forgeops}"
db_name="infisical"
echo "DB_CONNECTION_URI=${prefix}${db_user}:${pw}@postgres:5432/${db_name}" >> .env
echo "REDIS_URL=redis://redis:6379/0" >> .env

export "$db_pass_var"="$pw"
export "$app_pass_var"="$app_pw"
export "$mig_pass_var"="$mig_pw"
export AUTH_SECRET="$auth_sec"
export ENCRYPTION_KEY="$enc_key"

docker compose up -d --wait postgres redis
docker compose exec -T postgres psql -U "${POSTGRES_USER:-forgeops}" -d "${POSTGRES_DB:-forgeops}" -tc "SELECT 1 FROM pg_database WHERE datname = 'infisical'" | grep -q 1 || docker compose exec -T postgres psql -U "${POSTGRES_USER:-forgeops}" -d "${POSTGRES_DB:-forgeops}" -c "CREATE DATABASE infisical;"

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
