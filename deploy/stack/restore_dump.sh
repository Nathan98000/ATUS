#!/bin/sh
# Restore a serving-DB dump into the production-shaped stack database.
#
# Runs in the `restore` service (profile `dump`, docker-compose.prod.yml) with
# the postgres:16 client tools, as neondb_owner (PGHOST/PGUSER/PGPASSWORD/
# PGDATABASE come from the service environment). Mirrors the production
# restore (ADR-011 "Release switch"): pg_restore --no-owner --no-acl, the API
# grants, then VACUUM (ANALYZE) so the planner has statistics before the first
# query. A database that already holds the atus schema is left untouched, so
# restarting the stack does not restore twice.
set -eu

dump=/seed/serving.dump

loaded=$(psql --no-psqlrc -tAc "SELECT to_regclass('atus.respondents') IS NOT NULL")
if [ "$loaded" = "t" ]; then
  echo "restore: schema atus already present; skipping"
  exit 0
fi

if [ ! -s "$dump" ]; then
  echo "restore: no dump found; set ATUS_STACK_DUMP to a serving pg_dump -Fc file" >&2
  exit 1
fi

echo "restore: pg_restore --no-owner --no-acl ($(du -h "$dump" | cut -f1))"
pg_restore --no-owner --no-acl --exit-on-error --single-transaction --dbname "$PGDATABASE" "$dump"
psql --no-psqlrc -v ON_ERROR_STOP=1 --quiet -f /deploy/postgres/grant_api_role.sql
vacuumdb --analyze --quiet --schema atus --dbname "$PGDATABASE"
echo "restore: done"
