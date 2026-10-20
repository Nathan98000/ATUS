#!/bin/sh
# First-start bootstrap of the production-shaped database (docker-compose.prod.yml).
#
# The postgres image runs this once, on an empty data volume, as the bootstrap
# superuser over the local socket. It recreates the Neon layout the API sees in
# production (ADR-011 "Roles"):
#
#   neondb_owner  LOGIN, not a superuser; owns database `atus`, loads or
#                 restores the data (Neon's console-created owner role)
#   atus_api      LOGIN, SELECT only, the role the API connects as, with the
#                 provisional role defaults from ADR-011 "Bounded work"
#
# Default privileges grant atus_api read access to every schema and table
# neondb_owner creates later (migrations, `atus load`, `pg_restore --no-owner`),
# and deploy/postgres/grant_api_role.sql re-applies the grants explicitly after
# each load. Passwords come from the stack's env file and are never echoed.
set -eu

: "${ATUS_STACK_OWNER_PASSWORD:?set ATUS_STACK_OWNER_PASSWORD in the stack env file}"
: "${ATUS_STACK_API_DB_PASSWORD:?set ATUS_STACK_API_DB_PASSWORD in the stack env file}"

# \getenv reads the passwords inside psql, so they never appear in argv.
psql -v ON_ERROR_STOP=1 --no-psqlrc --quiet \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv owner_password ATUS_STACK_OWNER_PASSWORD
\getenv api_password ATUS_STACK_API_DB_PASSWORD
CREATE ROLE neondb_owner LOGIN CREATEDB CREATEROLE NOSUPERUSER PASSWORD :'owner_password';
CREATE ROLE atus_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
    PASSWORD :'api_password';

-- ADR-011 "Bounded work": backstops that hold even if the API forgets a
-- per-request SET LOCAL. jit=off matches Neon, whose Postgres has no LLVM
-- (audit F37).
ALTER ROLE atus_api SET statement_timeout = '85s';
ALTER ROLE atus_api SET lock_timeout = '5s';
ALTER ROLE atus_api SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE atus_api SET jit = off;

CREATE DATABASE atus OWNER neondb_owner;

-- Only the two roles may connect; PUBLIC loses CONNECT and TEMPORARY, so the
-- API role cannot even create temporary tables.
REVOKE ALL ON DATABASE atus FROM PUBLIC;
GRANT CONNECT ON DATABASE atus TO atus_api;

\connect atus
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner GRANT USAGE ON SCHEMAS TO atus_api;
ALTER DEFAULT PRIVILEGES FOR ROLE neondb_owner GRANT SELECT ON TABLES TO atus_api;
SQL

echo "initdb: created roles neondb_owner, atus_api and database atus"
