-- Read-only grants for the API role, applied after every load or restore of
-- the production-shaped stack database (deploy/stack/seed_fixture.py and
-- deploy/stack/restore_dump.sh). Idempotent. Run as neondb_owner.
--
-- The default privileges set at bootstrap (deploy/postgres/initdb/10-roles.sh)
-- already cover objects neondb_owner creates; this makes the result
-- independent of how the objects were created.
GRANT USAGE ON SCHEMA atus TO atus_api;
GRANT SELECT ON ALL TABLES IN SCHEMA atus TO atus_api;
