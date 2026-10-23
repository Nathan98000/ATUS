-- roles.sql: least-privilege roles for a serving database
-- (docs/adr/ADR-011-hosting.md "Roles", docs/adr/ADR-013-data-releases-and-serving-db.md).
--
-- Run it as the database owner, the role that restored the dump (on Neon the
-- console's neondb_owner), with psql 15 or newer:
--
--     ATUS_API_ROLE_PASSWORD=... psql -X -v ON_ERROR_STOP=1 -v api_role=atus_api \
--         -f scripts/ops/roles.sql
--
-- scripts/ops/restore_serving_db.sh runs it for you. The API role's password
-- comes from the psql variable api_password or, failing that, from the
-- environment variable ATUS_API_ROLE_PASSWORD; it is never written in this
-- file. Pass a SCRAM-SHA-256 verifier (the restore script does): PostgreSQL
-- stores it as is, so the plaintext never reaches the server, its logs or its
-- statement statistics. Running the script again is safe. It refuses a
-- database whose schema atus lacks the serving marker (the full database,
-- for one), before changing anything.
--
-- Result:
--   * the owner keeps owning every object: it restores, runs VACUUM (ANALYZE)
--     and `atus validate-analytics` (which writes atus.validation_results);
--   * :api_role (default atus_api) can LOG IN and SELECT the ten served tables
--     and nothing else: no INSERT, UPDATE, DELETE or TRUNCATE, no DDL, no
--     temporary tables, no metadata tables (validation_results, source_files,
--     schema_migrations), no role memberships;
--   * role defaults bound every API session (ADR-011 "Bounded work"):
--     statement_timeout 85 s (the API's per-request value, calibrated in
--     ADR-012 "Calibration"), lock_timeout 5 s,
--     idle_in_transaction_session_timeout 30 s, jit off (F37), and read-only
--     transactions by default (a second guard; the privileges are the boundary).
--     Every other setting of the role, role-wide or for this database, is
--     reset first, and the script fails unless exactly these five remain
--     (review r3: the role can set its own defaults).
--     max_parallel_workers_per_gather is not set here: the emulation load
--     tests left it at the server default, and the API can set it per
--     request (ATUS_API_MAX_PARALLEL_WORKERS; F38);
--   * no large objects. No privilege stops a role from creating them, so the
--     API role's are removed (a rotation runs this script) and the script
--     fails if any other remain (review r4; security.md R16).

\set ON_ERROR_STOP on
-- Errors print their message only: no statement text, no context.
\set VERBOSITY terse
\set SHOW_CONTEXT never

\if :{?api_role}
\else
  \set api_role atus_api
\endif
\if :{?api_password}
\else
  \getenv api_password ATUS_API_ROLE_PASSWORD
\endif
\if :{?api_password}
\else
  DO $$ BEGIN RAISE EXCEPTION 'roles.sql: no API password (set ATUS_API_ROLE_PASSWORD or -v api_password=...)'; END $$;
\endif
SELECT length(:'api_password') >= 16 AS password_ok \gset
\if :password_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'roles.sql: the API password is shorter than 16 characters'; END $$;
\endif
-- Only a serving database (the builder's marker on schema atus survives
-- pg_dump/pg_restore): never revoke PUBLIC's privileges on the full database.
SELECT coalesce(obj_description(to_regnamespace('atus')::oid, 'pg_namespace')
                LIKE 'atus-serving%', false) AS serving_ok \gset
\if :serving_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'roles.sql: schema atus carries no serving marker; run it on a restored serving database only'; END $$;
\endif

BEGIN;

-- The role: can log in, holds no attribute beyond that. When the restore
-- gives an existing role a new password, it first locks the role out
-- (NOLOGIN) and ends its sessions, so none is left to set the password or
-- settings again after this script (review r3); LOGIN here lets it back in.
SELECT format('CREATE ROLE %I', :'api_role')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'api_role') \gexec
ALTER ROLE :"api_role" WITH LOGIN NOCREATEDB NOCREATEROLE CONNECTION LIMIT 10
    PASSWORD :'api_password';

-- Only these defaults. A role may set its own (ALTER ROLE ... [IN DATABASE
-- ...] SET), so whoever held its password could have left planner switches,
-- work_mem or output settings that every later API session inherits: every
-- setting of the role is reset first, here and in this database (review r3).
ALTER ROLE :"api_role" RESET ALL;
ALTER ROLE :"api_role" IN DATABASE :"DBNAME" RESET ALL;
ALTER ROLE :"api_role" SET statement_timeout = '85s';
ALTER ROLE :"api_role" SET lock_timeout = '5s';
ALTER ROLE :"api_role" SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE :"api_role" SET jit = off;
ALTER ROLE :"api_role" SET default_transaction_read_only = on;

-- Large objects. Any role can create them: lo_create, lo_from_bytea, lowrite
-- and lo_put are EXECUTE to PUBLIC, creating one needs no privilege, and only
-- a superuser could revoke that (Neon has none). The API role can turn its
-- read-only default off, so whoever held its password could have stored data
-- in pg_largeobject, owned by the role, up to the project's storage limit
-- (security.md R16). A serving database has none: remove the role's, acting
-- as the role (only its owner may unlink one) through the ADMIN OPTION this
-- script's role holds on a role it created (PostgreSQL 16), then give that
-- membership back. The check below fails if any large object remains (review r4).
SELECT EXISTS (
    SELECT FROM pg_largeobject_metadata
    WHERE lomowner = (SELECT oid FROM pg_roles WHERE rolname = :'api_role')
) AS api_large_objects \gset
\if :api_large_objects
  SELECT pg_has_role(current_user, :'api_role', 'MEMBER WITH ADMIN OPTION') AS can_act_as_api \gset
  \if :can_act_as_api
  \else
    DO $$ BEGIN RAISE EXCEPTION 'roles.sql: the API role owns large objects, and this role holds no ADMIN OPTION on it to remove them; restore the release into a new project (security.md R16)'; END $$;
  \endif
  GRANT :"api_role" TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
  SET LOCAL ROLE :"api_role";
  SELECT count(lo_unlink(oid)) AS api_large_objects_removed
  FROM pg_largeobject_metadata
  WHERE lomowner = (SELECT oid FROM pg_roles WHERE rolname = current_user) \gset
  RESET ROLE;
  REVOKE :"api_role" FROM CURRENT_USER;
\endif

-- Database and schemas: connect and look up names, never create anything.
REVOKE CREATE, TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO :"api_role";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA atus FROM PUBLIC;
GRANT USAGE ON SCHEMA atus TO :"api_role";

-- Tables: SELECT on exactly the served tables (the serving contract,
-- src/atus_pipeline/serving/contract.py READ; a unit test keeps this list equal).
REVOKE ALL ON ALL TABLES IN SCHEMA atus FROM PUBLIC, :"api_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA atus FROM PUBLIC, :"api_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, :"api_role";
GRANT SELECT ON TABLE
    atus.respondents,
    atus.household_members,
    atus.cps_persons,
    atus.activities,
    atus.replicate_weights,
    atus.pandemic_replicate_weights,
    atus.activity_tier1,
    atus.activity_tier2,
    atus.activity_codes,
    atus.ingestion_runs
TO :"api_role";

-- Verify the result instead of trusting it.
SELECT rolcanlogin AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication
                            OR rolbypassrls) AS attributes_ok
FROM pg_roles WHERE rolname = :'api_role' \gset
\if :attributes_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'roles.sql: the API role holds an attribute beyond LOGIN'; END $$;
\endif

-- Exactly the five defaults above, role-wide, and none for this database
-- (the restore checks the same: serving/restore.py EXPECTED_ROLE_SETTINGS).
SELECT coalesce((
    SELECT array_agg(setting ORDER BY setting COLLATE "C")
    FROM pg_db_role_setting s CROSS JOIN unnest(s.setconfig) AS setting
    WHERE s.setrole = (SELECT oid FROM pg_roles WHERE rolname = :'api_role')
      AND s.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))
), '{}') = ARRAY['default_transaction_read_only=on', 'idle_in_transaction_session_timeout=30s',
                 'jit=off', 'lock_timeout=5s', 'statement_timeout=85s']::text[]
AS settings_ok \gset
\if :settings_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'roles.sql: the API role has settings beyond its five defaults'; END $$;
\endif

SELECT NOT EXISTS (
    SELECT FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
    WHERE r.rolname = :'api_role'
) AS memberships_ok \gset
\if :memberships_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'roles.sql: the API role is a member of another role'; END $$;
\endif

SELECT NOT EXISTS (
    SELECT FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'])
        AS p (privilege)
    WHERE n.nspname IN ('atus', 'public') AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND has_table_privilege(:'api_role', c.oid, p.privilege)
) AND NOT EXISTS (
    SELECT FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    CROSS JOIN unnest(ARRAY['USAGE', 'UPDATE']) AS p (privilege)
    WHERE n.nspname IN ('atus', 'public') AND c.relkind = 'S'
      AND has_sequence_privilege(:'api_role', c.oid, p.privilege)
) AND NOT has_schema_privilege(:'api_role', 'atus', 'CREATE')
  AND NOT has_schema_privilege(:'api_role', 'public', 'CREATE')
  AND NOT has_database_privilege(:'api_role', current_database(), 'CREATE')
  AND NOT has_database_privilege(:'api_role', current_database(), 'TEMPORARY')
AS read_only_ok \gset
\if :read_only_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'roles.sql: the API role can still write or create objects'; END $$;
\endif

-- No privilege stops the API role from creating large objects (above): a
-- serving database holds none, so any that remain (another role's) fail here.
SELECT NOT EXISTS (SELECT FROM pg_largeobject_metadata) AS large_objects_ok \gset
\if :large_objects_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'roles.sql: the database holds large objects, which a serving database never has; remove them as their owner (SELECT lo_unlink(oid) FROM pg_largeobject_metadata) or restore the release into a new project'; END $$;
\endif

SELECT count(*) = 10 AS reads_ok
FROM unnest(ARRAY[
    'atus.respondents', 'atus.household_members', 'atus.cps_persons', 'atus.activities',
    'atus.replicate_weights', 'atus.pandemic_replicate_weights', 'atus.activity_tier1',
    'atus.activity_tier2', 'atus.activity_codes', 'atus.ingestion_runs'
]) AS t (name)
WHERE has_table_privilege(:'api_role', t.name, 'SELECT') \gset
\if :reads_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'roles.sql: the API role cannot read every served table'; END $$;
\endif

COMMIT;

-- Reported only once committed: a failed check above rolls the removal back.
\if :api_large_objects
  \echo roles.sql: removed :api_large_objects_removed large object(s) that :api_role had stored.
\endif
\echo roles.sql: :api_role can log in and read the served tables; it cannot write any table, create tables or schemas, or use temporary tables. No privilege stops it from creating large objects (security.md R16); none exist now.
