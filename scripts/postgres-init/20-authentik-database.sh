#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# Give Authentik its own database on the shared Postgres server. design.md §13.3, task 6.3.
#
# §13.3 has Authentik "sharing the existing Postgres and Redis". Sharing the *server* is
# the intent; sharing the *database* would not be safe. Authentik manages its own schema
# with its own migrations — around ninety tables — and putting them in `forgeops`
# alongside the Alembic-managed ones would make `alembic revision --autogenerate` propose
# dropping every one of them. `tests/integration/test_alembic_autogenerate_clean.py`
# exists to catch precisely that class of drift, so the arrangement that trips it is the
# wrong arrangement.
#
# Runs after `10-forgeops-roles.sh` (lexical order) and, like it, is a no-op with a clear
# message when its password is absent rather than inventing a default.
#
# What this does NOT claim: it does not isolate Authentik from the `forgeops` database.
# PostgreSQL grants CONNECT on every database to PUBLIC by default, so a REVOKE aimed at
# one role would achieve nothing while reading as though it had. Revoking from PUBLIC
# instead would affect every role including the application's. Stating the limit is
# better than shipping a line that looks like a control and is not; real isolation is a
# separate server, which is what a production deployment uses.
set -euo pipefail

authentik_password="${AUTHENTIK_POSTGRESQL__PASSWORD:-}"
authentik_user="${AUTHENTIK_POSTGRESQL__USER:-authentik}"
authentik_db="${AUTHENTIK_POSTGRESQL__NAME:-authentik}"

if [ -z "$authentik_password" ]; then
  echo "forgeops: AUTHENTIK_POSTGRESQL__PASSWORD is unset; skipping Authentik database" >&2
  echo "forgeops: creation. The stack still starts, but authentik-server will fail to" >&2
  echo "forgeops: connect and login will be unavailable (design 13.3)." >&2
  exit 0
fi

# `psql -v` with `:'name'` quotes as a SQL literal and `:"name"` as an identifier, so
# neither the password nor the names can terminate a statement. Nothing is interpolated
# into the SQL text.
#
# CREATE DATABASE runs in no transaction block, so it cannot be made conditional inside a
# DO block. `\gexec` — psql executing the rows a query returns — is the standard way to
# express "create it only if absent" in plain SQL.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v ak_password="$authentik_password" -v ak_user="$authentik_user" -v ak_db="$authentik_db" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'ak_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ak_user')
\gexec

ALTER ROLE :"ak_user" LOGIN PASSWORD :'ak_password';

SELECT format('CREATE DATABASE %I OWNER %I', :'ak_db', :'ak_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'ak_db')
\gexec
SQL

echo "forgeops: Authentik database '${authentik_db}' is ready (design 13.3)"
