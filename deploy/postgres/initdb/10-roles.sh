#!/bin/sh
# First-start bootstrap of the production-shaped database (docker-compose.prod.yml).
#
# The postgres image runs this once, on an empty data volume, as the bootstrap
# superuser over the local socket. It recreates what Neon's console creates
# for a new project, and nothing more (ADR-011 "Roles"):
#
#   neondb_owner  LOGIN, CREATEDB, CREATEROLE, not a superuser: Neon's
#                 console-created owner role, which loads or restores the data
#   atus          the database, owned by neondb_owner
#
# The API role is not created here. Once the data is in (the `seed` service
# builds a serving database, or `restore` restores a dump), the `roles` service
# applies scripts/ops/roles.sql as neondb_owner, exactly as the production
# restore does: that script is the single definition of `atus_api`, its
# grants and its role defaults. The password comes from the stack's env file
# and is never echoed.
set -eu

: "${ATUS_STACK_OWNER_PASSWORD:?set ATUS_STACK_OWNER_PASSWORD in the stack env file}"

# \getenv reads the password inside psql, so it never appears in argv.
psql -v ON_ERROR_STOP=1 --no-psqlrc --quiet \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv owner_password ATUS_STACK_OWNER_PASSWORD
CREATE ROLE neondb_owner LOGIN CREATEDB CREATEROLE NOSUPERUSER PASSWORD :'owner_password';
CREATE DATABASE atus OWNER neondb_owner;
SQL

echo "initdb: created role neondb_owner and database atus (scripts/ops/roles.sql adds atus_api)"
