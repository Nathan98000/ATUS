"""The production stack's database has the roles and settings it claims (opt-in).

Starts only `db`, `seed` and `roles` from docker-compose.prod.yml (a serving
database built from the fixture, throwaway secrets, a random loopback port).
The API role comes from scripts/ops/roles.sql, applied exactly as the
production restore applies it, so these checks hold for the deployed role:

- `atus_api` reads exactly the served tables, and cannot write, truncate,
  create schemas or tables (permanent or temporary) or alter other roles:
  the privileges are the boundary, so every write is tried in an explicitly
  READ WRITE transaction. Its transactions are also read-only by default (a
  second guard), and it carries ADR-011's role defaults. Those defaults are
  a backstop, not a privilege boundary: any session may SET them (and
  Postgres lets a role change its own defaults);
- no privilege stops it from creating large objects (security.md R16):
  re-applying roles.sql removes the ones it stored (review r4);
- `neondb_owner` owns the data but is not a superuser (as on Neon);
- the database is a serving build (marker, serving data_version);
- the server runs Neon's settings for the 0.25-2 CU compute with jit off (F37).

ATUS_DOCKER_TESTS=1 opts in (see conftest.py).
"""

from __future__ import annotations

import re
import secrets
import subprocess

import pytest

from .conftest import REPO_ROOT

psycopg = pytest.importorskip("psycopg")

SERVED = {
    "respondents", "household_members", "cps_persons", "activities", "replicate_weights",
    "pandemic_replicate_weights", "activity_tier1", "activity_tier2", "activity_codes",
    "ingestion_runs",
}

WRITES = [
    "INSERT INTO atus.respondents SELECT * FROM atus.respondents LIMIT 1",
    "UPDATE atus.respondents SET tucaseid = tucaseid",
    "DELETE FROM atus.respondents",
    "TRUNCATE atus.respondents",
    "CREATE TABLE atus.api_was_here (x int)",
    "CREATE TABLE public.api_was_here (x int)",
    "CREATE TEMP TABLE api_was_here (x int)",
    "CREATE SCHEMA api_was_here",
    "ALTER ROLE neondb_owner NOLOGIN",
]


@pytest.fixture(scope="module")
def stack(docker_daemon, api_image, docker_prefix, tmp_path_factory):
    """db + seed of the production stack; yields (port, passwords); always torn down."""
    passwords = {name: secrets.token_hex(16) for name in ("superuser", "owner", "api")}
    env_file = tmp_path_factory.mktemp("roles") / "stack.env"
    env_file.touch(mode=0o600)
    env_file.write_text(
        f"ATUS_STACK_PG_SUPERUSER_PASSWORD={passwords['superuser']}\n"
        f"ATUS_STACK_OWNER_PASSWORD={passwords['owner']}\n"
        f"ATUS_STACK_API_DB_PASSWORD={passwords['api']}\n"
        f"ATUS_STACK_EDGE_SECRET={secrets.token_hex(16)}\n"
        f"ATUS_STACK_OPS_TOKEN={secrets.token_hex(16)}\n"
        "ATUS_STACK_DB_PORT=\n"
        f"ATUS_STACK_IMAGE={api_image}\n"
    )
    compose = ["docker", "compose", "-p", f"{docker_prefix}-roles", "--env-file",
               str(env_file), "-f", str(REPO_ROOT / "docker-compose.prod.yml")]
    try:
        # `run` starts db and seed first (roles depends on both).
        result = subprocess.run([*compose, "run", "--rm", "roles"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=600, check=False)
        assert result.returncode == 0, result.stderr[-3000:]
        assert "roles.sql: atus_api can log in and read the served tables" in result.stdout
        port = subprocess.run([*compose, "port", "db", "5432"], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=True).stdout
        yield int(port.strip().rsplit(":", 1)[1]), passwords, compose
    finally:
        subprocess.run([*compose, "down", "-v", "--remove-orphans"], cwd=REPO_ROOT,
                       capture_output=True, check=False)


def _connect(stack, user: str):
    port, passwords, _ = stack
    password = passwords["api" if user == "atus_api" else "owner"]
    return psycopg.connect(host="127.0.0.1", port=port, dbname="atus", user=user,
                           password=password, autocommit=True, connect_timeout=10)


def test_api_role_reads_the_data(stack):
    with _connect(stack, "atus_api") as conn:
        assert conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0] > 0


@pytest.mark.parametrize("statement", WRITES)
def test_api_role_cannot_write(stack, statement):
    """Privileges alone: the read-only transaction default is switched off first."""
    with _connect(stack, "atus_api") as conn:
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


LARGE_OBJECT_WRITES = ["SELECT lo_from_bytea(0, '\\x00')", "SELECT lo_create(0)"]


@pytest.mark.parametrize("statement", LARGE_OBJECT_WRITES)
def test_api_role_can_still_create_large_objects(stack, statement):
    """Review r4: lo_create, lo_from_bytea, lowrite and lo_put are EXECUTE to PUBLIC
    and creating a large object needs no privilege, so once its read-only default
    is off the role creates one (rolled back here). The documented exposure
    (security.md R16), bounded by roles.sql, which removes the role's (below)."""
    with _connect(stack, "atus_api") as conn:
        conn.execute("SET default_transaction_read_only = off")
        with conn.transaction(force_rollback=True):
            assert conn.execute(statement).fetchone()[0] > 0


def test_reapplying_roles_removes_the_api_roles_large_objects(stack):
    with _connect(stack, "atus_api") as conn:
        conn.execute("SET default_transaction_read_only = off")
        for statement in LARGE_OBJECT_WRITES:
            conn.execute(statement)
    with _connect(stack, "neondb_owner") as conn:
        assert conn.execute("SELECT count(*) FROM pg_largeobject_metadata").fetchone()[0] == 2
    result = subprocess.run([*stack[2], "run", "--rm", "--no-deps", "roles"], cwd=REPO_ROOT,
                            capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "roles.sql: removed 2 large object(s) that atus_api had stored." in result.stdout
    with _connect(stack, "neondb_owner") as conn:
        assert conn.execute("SELECT count(*) FROM pg_largeobject_metadata").fetchone()[0] == 0


def test_api_role_transactions_are_read_only_by_default(stack):
    with _connect(stack, "atus_api") as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(WRITES[0])


def test_api_role_reads_exactly_the_served_tables(stack):
    with _connect(stack, "atus_api") as conn:
        readable = {
            name for (name,) in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'atus' AND c.relkind = 'r' "
                "AND has_table_privilege(c.oid, 'SELECT')"
            )
        }
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM atus.validation_results")
    assert readable == SERVED


def test_api_role_carries_the_adr011_defaults(stack):
    with _connect(stack, "atus_api") as conn:
        settings = {
            name: conn.execute(f"SHOW {name}").fetchone()[0]
            for name in ("statement_timeout", "lock_timeout",
                         "idle_in_transaction_session_timeout", "jit",
                         "default_transaction_read_only")
        }
        flags = conn.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, "
            "rolconnlimit FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        memberships = conn.execute(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname = current_user"
        ).fetchone()[0]
    assert settings == {"statement_timeout": "85s", "lock_timeout": "5s",
                        "idle_in_transaction_session_timeout": "30s", "jit": "off",
                        "default_transaction_read_only": "on"}
    assert flags == (False, False, False, False, False, 10)
    assert memberships == 0


def test_the_database_is_a_serving_build(stack):
    with _connect(stack, "atus_api") as conn:
        marker = conn.execute(
            "SELECT obj_description('atus'::regnamespace, 'pg_namespace')").fetchone()[0]
        version = conn.execute(
            "SELECT data_version FROM atus.ingestion_runs WHERE status = 'succeeded' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert marker.startswith("atus-serving")
    assert re.fullmatch(r"\d{4}:serving-\d{8}T\d{6}Z-[0-9a-f]{8}:[0-9a-f]{12}", version)


def test_owner_owns_the_data_but_is_not_a_superuser(stack):
    with _connect(stack, "neondb_owner") as conn:
        superuser = conn.execute("SHOW is_superuser").fetchone()[0]
        owner = conn.execute(
            "SELECT tableowner FROM pg_tables WHERE schemaname = 'atus' "
            "AND tablename = 'respondents'"
        ).fetchone()[0]
    assert superuser == "off" and owner == "neondb_owner"


def test_server_runs_neons_settings_with_jit_off(stack):
    with _connect(stack, "neondb_owner") as conn:
        values = {
            name: conn.execute(f"SHOW {name}").fetchone()[0]
            for name in ("jit", "shared_buffers", "max_connections", "max_worker_processes",
                         "max_parallel_workers")
        }
    assert values == {"jit": "off", "shared_buffers": "215MB", "max_connections": "839",
                      "max_worker_processes": "17", "max_parallel_workers": "8"}
