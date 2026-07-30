#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# Give the two ForgeOps database roles a local login. design.md §6.4, §6.7.
#
# §6.4 states the two-role arrangement explicitly "because it is easy to lose":
# `DATABASE_URL` connects as `forgeops_app`, which cannot UPDATE or DELETE audit
# rows, and `ALEMBIC_DATABASE_URL` connects as `forgeops_migrator`, which owns the
# schema. A single-role deployment silently defeats mechanism 3 — every trigger is
# still installed, every test still passes, and the application can drop the triggers
# whenever it likes, because dropping a trigger needs the ownership a merged role
# holds. `scripts/check-db-roles.py` is what turns that into a build failure.
#
# The split of responsibility between this script and migration `0002` is the point:
#
#   * `0002` CREATES both roles with **no password**, because a migration is committed
#     source and a role created with a password there would be a committed credential;
#   * this script — which runs from `/docker-entrypoint-initdb.d/`, reads the
#     untracked `.env`, and is never itself a credential — grants LOGIN and a
#     password. A real deployment does the same from its own secret store.
#
# Ordering: Postgres runs this at first initialisation, BEFORE any migration. So the
# roles are created here with LOGIN and a password, and `0002`'s idempotent `DO`
# block then finds them present, skips creation, and grants privileges. Neither half
# assumes the other ran first.
#
# The script is a no-op when the passwords are absent, and says so, rather than
# inventing a default. A default password for a role that can write to a developer's
# filesystem is worse than a missing one.
set -euo pipefail

app_password="${FORGEOPS_APP_DB_PASSWORD:-}"
migrator_password="${FORGEOPS_MIGRATOR_DB_PASSWORD:-}"

if [ -z "$app_password" ] || [ -z "$migrator_password" ]; then
  echo "forgeops: FORGEOPS_APP_DB_PASSWORD / FORGEOPS_MIGRATOR_DB_PASSWORD are unset;" >&2
  echo "forgeops: skipping role login setup. The stack still starts, but the app will" >&2
  echo "forgeops: connect as the single POSTGRES_USER and scripts/check-db-roles.py" >&2
  echo "forgeops: will report the two-role arrangement (design 6.4) as not in effect." >&2
  exit 0
fi

# `psql -v` + `:'name'` quotes the value as a SQL literal, so a password containing a
# quote cannot terminate the statement. Never interpolate it into the SQL text.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_password="$app_password" -v migrator_password="$migrator_password" \
  -v db="$POSTGRES_DB" <<'SQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'forgeops_app') THEN
        CREATE ROLE forgeops_app LOGIN;
    ELSE
        ALTER ROLE forgeops_app LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'forgeops_migrator') THEN
        CREATE ROLE forgeops_migrator LOGIN;
    ELSE
        ALTER ROLE forgeops_migrator LOGIN;
    END IF;
END
$$;

ALTER ROLE forgeops_app      PASSWORD :'app_password';
ALTER ROLE forgeops_migrator PASSWORD :'migrator_password';

GRANT CONNECT ON DATABASE :"db" TO forgeops_app, forgeops_migrator;

-- The migrator owns the schema and creates every table. It needs CREATE on `public`
-- BEFORE the first migration runs, and since PostgreSQL 15 `public` no longer grants
-- CREATE to PUBLIC — so without this line `0001` cannot run as the migrator at all,
-- and `0002`'s own grant would never be reached. That ordering trap is why the grant
-- lives here rather than in a migration.
GRANT USAGE, CREATE ON SCHEMA public TO forgeops_migrator;
GRANT USAGE          ON SCHEMA public TO forgeops_app;

-- Whatever the migrator creates, the application may read and write by default.
-- `0007` then REVOKEs the three verbs that would let it rewrite the audit log, so
-- immutability is expressed as a narrowing of a permissive default rather than as a
-- grant each later revision has to remember.
ALTER DEFAULT PRIVILEGES FOR ROLE forgeops_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO forgeops_app;
ALTER DEFAULT PRIVILEGES FOR ROLE forgeops_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO forgeops_app;

-- `CREATE EXTENSION` requires superuser for these three, so the migrator cannot do
-- it: `0001`'s `CREATE EXTENSION IF NOT EXISTS vector` would fail with "Must be
-- superuser to create this extension" the moment migrations stop running as the
-- Postgres superuser. Creating them here makes each migration's `IF NOT EXISTS` a
-- no-op, so a superuser-run migration still works standalone and a migrator-run one
-- works too. Extensions are per-database, which is why this is scoped to POSTGRES_DB.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL

echo "forgeops: forgeops_app and forgeops_migrator can now log in (design 6.4, 6.7)"
