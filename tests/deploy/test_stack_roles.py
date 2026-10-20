"""The production stack's database has the roles and settings it claims (opt-in).

Starts only `db` and `seed` from docker-compose.prod.yml (fixture data, throwaway
secrets, a random loopback port), then connects from the host as each role:

- `atus_api` can read but cannot write, truncate, create schemas or tables
  (permanent or temporary) or alter other roles, and carries ADR-011's role
  defaults. Those defaults are a backstop, not a privilege boundary: any
  session may SET them (and Postgres lets a role change its own defaults);
- `neondb_owner` owns the data but is not a superuser (as on Neon);
- the server runs Neon's settings for the 0.25-2 CU compute with jit off (F37).

This is the local stand-in. The deployed roles come from scripts/ops/roles.sql
(serving-DB work), which needs its own test against the same rules.
ATUS_DOCKER_TESTS=1 opts in (see conftest.py).
"""

from __future__ import annotations

import secrets
import subprocess

import pytest

from .conftest import REPO_ROOT

psycopg = pytest.importorskip("psycopg")

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
        result = subprocess.run([*compose, "run", "--rm", "seed"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=600, check=False)
        assert result.returncode == 0, result.stderr[-3000:]
        port = subprocess.run([*compose, "port", "db", "5432"], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=True).stdout
        yield int(port.strip().rsplit(":", 1)[1]), passwords
    finally:
        subprocess.run([*compose, "down", "-v", "--remove-orphans"], cwd=REPO_ROOT,
                       capture_output=True, check=False)


def _connect(stack, user: str):
    port, passwords = stack
    password = passwords["api" if user == "atus_api" else "owner"]
    return psycopg.connect(host="127.0.0.1", port=port, dbname="atus", user=user,
                           password=password, autocommit=True, connect_timeout=10)


def test_api_role_reads_the_data(stack):
    with _connect(stack, "atus_api") as conn:
        assert conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0] > 0


@pytest.mark.parametrize("statement", WRITES)
def test_api_role_cannot_write(stack, statement):
    with _connect(stack, "atus_api") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


def test_api_role_carries_the_adr011_defaults(stack):
    with _connect(stack, "atus_api") as conn:
        settings = {
            name: conn.execute(f"SHOW {name}").fetchone()[0]
            for name in ("statement_timeout", "lock_timeout",
                         "idle_in_transaction_session_timeout", "jit")
        }
        flags = conn.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolinherit FROM pg_roles "
            "WHERE rolname = current_user"
        ).fetchone()
    assert settings == {"statement_timeout": "85s", "lock_timeout": "5s",
                        "idle_in_transaction_session_timeout": "30s", "jit": "off"}
    assert flags == (False, False, False, False)


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
