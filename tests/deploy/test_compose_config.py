"""The compose files resolve to the intended stacks (F09, F32, F33, F37).

Validated with `docker compose config`, which parses, interpolates and merges
the files exactly as `up` would, without contacting the Docker daemon.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from urllib.parse import urlsplit

import pytest

from .conftest import DUMMY_STACK_ENV, REPO_ROOT, run_compose_config

PROD = "docker-compose.prod.yml"
EMULATION = "docker-compose.emulation.yml"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
MIB = 1024 * 1024


def _neon_settings_module():
    spec = importlib.util.spec_from_file_location(
        "neon_settings", REPO_ROOT / "deploy" / "emulation" / "neon_settings.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


def _published(service: dict) -> list[dict]:
    """Every `ports:` entry publishes on the host (an empty host port = a random one)."""
    return service.get("ports", [])


def _db_settings(service: dict) -> dict[str, str]:
    command = service["command"]
    pairs = [command[i + 1] for i, arg in enumerate(command) if arg == "-c"]
    return dict(pair.split("=", 1) for pair in pairs)


@pytest.fixture(scope="module")
def dev_config(compose_config):
    return compose_config("docker-compose.yml")


@pytest.fixture(scope="module")
def prod_config(compose_config):
    return compose_config(PROD, env=DUMMY_STACK_ENV,
                          profiles=("dump", "snapshot", "edge", "smoke"))


@pytest.fixture(scope="module")
def emulation_config(compose_config):
    return compose_config(PROD, EMULATION, env=DUMMY_STACK_ENV, profiles=("pgbouncer",))


# --- development database (docker-compose.yml) ----------------------------------


class TestDevelopmentCompose:
    @pytest.fixture
    def config(self, dev_config):
        return dev_config


    def test_project_and_volume_names_are_fixed(self, config):
        """Every checkout resolves the volume the original ATUS/ directory named."""
        assert config["name"] == "atus"
        assert config["volumes"] == {"atus_pgdata": {"name": "atus_atus_pgdata"}}
        postgres = config["services"]["postgres"]
        assert postgres["container_name"] == "atus-postgres"
        assert postgres["volumes"][0]["source"] == "atus_pgdata"

    def test_project_name_does_not_depend_on_the_directory(self, compose_cli):
        result = run_compose_config("docker-compose.yml", project=None)
        assert result.returncode == 0, result.stderr
        assert '"name": "atus"' in result.stdout

    def test_postgres_is_pinned_and_bound_to_loopback(self, config):
        postgres = config["services"]["postgres"]
        assert postgres["image"].startswith("postgres:16@") and DIGEST.search(postgres["image"])
        ports = _published(postgres)
        assert [(p["host_ip"], p["published"], p["target"]) for p in ports] == [
            ("127.0.0.1", "5434", 5432)
        ]

    def test_shared_memory_is_raised(self, config):
        assert int(config["services"]["postgres"]["shm_size"]) >= 512 * MIB


# --- production stack (docker-compose.prod.yml) ---------------------------------


class TestProductionCompose:
    @pytest.fixture
    def config(self, prod_config):
        return prod_config


    def test_secrets_are_required(self, compose_cli):
        result = run_compose_config(PROD, env={})
        assert result.returncode != 0
        assert "deploy/stack/stack.env.example" in result.stderr

    def test_every_image_is_digest_pinned_or_built_here(self, config):
        for name, service in config["services"].items():
            image = service["image"]
            built = "build" in service or image == "atus-api:local"
            assert DIGEST.search(image) or built, f"{name}: {image}"

    def test_project_is_separate_from_the_development_database(self, config):
        assert config["name"] == "atus-stack"
        assert set(config["volumes"]) == {"pgdata"}

    def test_only_loopback_ports_and_the_api_is_not_published(self, config):
        for name, service in config["services"].items():
            for port in _published(service):
                assert port["host_ip"] == "127.0.0.1", f"{name}: {port}"
        assert _published(config["services"]["api"]) == []

    def test_api_runs_as_deployed(self, config):
        api = config["services"]["api"]
        env = api["environment"]
        assert env["ATUS_ENV"] == "production"
        assert env["ATUS_DATABASE_URL"] == "postgresql://atus_api:dummy-api@db:5432/atus"
        # The one setting Render never has: the stack's Postgres has no TLS.
        assert env["ATUS_DATABASE_TLS"] == "local-stack-only"
        assert env["ATUS_EDGE_SECRETS"] == "dummy-edge"
        assert env["ATUS_OPS_TOKEN"] == "dummy-ops"
        assert env["WEB_CONCURRENCY"] == "1"
        assert env["PORT"] == "10000"
        assert api["read_only"] is True
        assert api["cap_drop"] == ["ALL"]
        # the API starts only once roles.sql has created its role
        assert api["depends_on"]["roles"]["condition"] == "service_completed_successfully"

    @pytest.mark.parametrize("route", ["db", "pgbouncer"])
    def test_api_environment_passes_the_production_checks(self, compose_config, route):
        """The stack's API settings are accepted by the API's own production
        validation (TLS rule included), directly and through PgBouncer, and
        would be refused without the local-stack escape hatch."""
        from atus_pipeline.config import ConfigError, check_api_settings, load_settings

        env = dict(DUMMY_STACK_ENV, ATUS_STACK_EDGE_SECRET="e" * 40,
                   ATUS_STACK_OPS_TOKEN="o" * 40)
        if route == "pgbouncer":
            env.update(ATUS_STACK_API_DB_HOST="pgbouncer", ATUS_STACK_API_DB_HOST_PORT="6432")
        config = compose_config(PROD, EMULATION, env=env, profiles=("snapshot", "pgbouncer"))
        for service in ("api", "snapshot-api"):
            api_env = config["services"][service]["environment"]
            check_api_settings(load_settings(api_env))
            without_hatch = {k: v for k, v in api_env.items() if k != "ATUS_DATABASE_TLS"}
            with pytest.raises(ConfigError, match="sslmode"):
                load_settings(without_hatch)

    def test_api_runs_the_image_built_from_this_checkout_by_default(self, config):
        api = config["services"]["api"]
        assert api["image"] == "atus-api:local"
        assert api["pull_policy"] == "build"
        assert api["build"]["context"] == str(REPO_ROOT)
        assert set(api["build"]["args"]) == {"ATUS_BUILD_SHA", "ATUS_VERSION"}

    def test_api_image_can_be_a_registry_digest(self, compose_config):
        image = "ghcr.io/nathan98000/atus-api@sha256:" + "a" * 64
        env = dict(DUMMY_STACK_ENV, ATUS_STACK_IMAGE=image, ATUS_STACK_PULL_POLICY="missing")
        config = compose_config(PROD, env=env)
        for name in ("api", "seed"):
            assert config["services"][name]["image"] == image
        assert config["services"]["api"]["pull_policy"] == "missing"

    def test_database_matches_neon(self, config):
        db = config["services"]["db"]
        expected = _neon_settings_module().neon_settings(0.25, 2.0)
        settings = _db_settings(db)
        assert settings == {
            "jit": "off",
            "shared_buffers": f"{expected.shared_buffers_mb}MB",
            "max_connections": str(expected.max_connections),
            "max_worker_processes": str(expected.max_worker_processes),
            "max_parallel_workers": str(expected.max_parallel_workers),
        }
        assert db["image"].startswith("postgres:16@")
        assert int(db["shm_size"]) >= 512 * MIB

    def test_database_bootstraps_only_what_neons_console_creates(self, config):
        db = config["services"]["db"]
        mounts = {m["target"]: m for m in db["volumes"]}
        initdb = mounts["/docker-entrypoint-initdb.d"]
        assert initdb["read_only"] is True
        assert initdb["source"] == str(REPO_ROOT / "deploy" / "postgres" / "initdb")
        # the owner only: the API role comes from scripts/ops/roles.sql
        assert "ATUS_STACK_API_DB_PASSWORD" not in db["environment"]
        bootstrap = (REPO_ROOT / "deploy" / "postgres" / "initdb" / "10-roles.sh").read_text()
        assert "CREATE ROLE neondb_owner" in bootstrap
        sql = bootstrap.split("<<'SQL'", 1)[1].split("\nSQL\n", 1)[0]
        assert "atus_api" not in sql and "GRANT" not in sql
        assert not (REPO_ROOT / "deploy" / "postgres" / "grant_api_role.sql").exists()

    def test_roles_sql_is_the_single_definition_of_the_api_role(self, config):
        roles = config["services"]["roles"]
        assert roles["depends_on"]["seed"]["condition"] == "service_completed_successfully"
        assert roles["image"].startswith("postgres:16@") and DIGEST.search(roles["image"])
        (mount,) = roles["volumes"]
        assert mount["source"] == str(REPO_ROOT / "scripts" / "ops" / "roles.sql")
        assert mount["read_only"] is True
        assert roles["entrypoint"][0] == "psql" and roles["entrypoint"][-1] == mount["target"]
        assert "ON_ERROR_STOP=1" in roles["entrypoint"]
        env = roles["environment"]
        assert env["PGUSER"] == "neondb_owner" and env["PGDATABASE"] == "atus"
        assert env["ATUS_API_ROLE_PASSWORD"] == DUMMY_STACK_ENV["ATUS_STACK_API_DB_PASSWORD"]
        assert roles["read_only"] is True and _published(roles) == []
        # nothing else in the stack defines roles or grants
        for path in (REPO_ROOT / "deploy").rglob("*"):
            if path.is_file() and path.suffix in (".sql", ".sh", ".py"):
                text = path.read_text()
                assert "GRANT SELECT" not in text and "CREATE ROLE atus_api" not in text, path
        # The superuser exists for initdb only; nothing else logs in with it.
        for name, service in config["services"].items():
            for value in (service.get("environment") or {}).values():
                if str(value).startswith("postgresql://"):
                    assert urlsplit(value).username != "postgres", name

    def test_seed_and_restore_use_the_owner_role(self, config):
        seed = config["services"]["seed"]
        assert seed["environment"]["ATUS_SEED_DATABASE_URL"].startswith(
            "postgresql://neondb_owner:"
        )
        # the loader, not the API: never production mode
        assert seed["environment"]["ATUS_ENV"] == "development"
        assert all(m["read_only"] for m in seed["volumes"])
        # The image ships no migrations; data commands must find the mounted ones.
        mounts = {m["target"]: m["source"] for m in seed["volumes"]}
        migrations = seed["environment"]["ATUS_MIGRATIONS_DIR"]
        assert mounts[migrations] == str(REPO_ROOT / "migrations")
        restore = config["services"]["restore"]
        assert restore["profiles"] == ["dump"]
        assert restore["environment"]["PGUSER"] == "neondb_owner"
        assert seed["depends_on"]["restore"]["required"] is False

    def test_smoke_probe_gets_the_api_secrets_it_checks(self, config):
        smoke = config["services"]["smoke"]
        assert smoke["profiles"] == ["smoke"]
        assert smoke["environment"] == {
            "ATUS_SMOKE_EDGE_SECRET": DUMMY_STACK_ENV["ATUS_STACK_EDGE_SECRET"],
            "ATUS_SMOKE_OPS_TOKEN": DUMMY_STACK_ENV["ATUS_STACK_OPS_TOKEN"],
        }
        assert _published(smoke) == [] and smoke["read_only"] is True

    def test_edge_runs_the_worker_in_front_of_the_api(self, config):
        edge = config["services"]["edge"]
        assert edge["profiles"] == ["edge"]
        assert edge["build"]["dockerfile"] == "deploy/edge/Dockerfile"
        env = edge["environment"]
        # The Worker refuses plain HTTP to a non-loopback origin; the entrypoint
        # relays loopback to the api container (deploy/edge/origin-relay.mjs).
        assert env["RENDER_ORIGIN"] == "http://127.0.0.1:10000"
        assert env["ATUS_EDGE_RELAY_TARGET"] == "api:10000"
        assert not {"ATUS_EDGE_UPSTREAM", "ATUS_EDGE_RELAY_LISTEN"} & set(env)
        assert env["EDGE_AUTH_SECRET"] == DUMMY_STACK_ENV["ATUS_STACK_EDGE_SECRET"]
        assert env["WRANGLER_SEND_METRICS"] == "false"
        assert {"DATA_VERSION", "ANALYTICS_VERSION", "SNAPSHOT_ID"} <= set(env)
        (assets,) = edge["volumes"]
        assert assets["target"] == "/work/frontend/dist" and assets["read_only"] is True
        assert assets["source"] == str(REPO_ROOT / "frontend" / "dist")
        assert [(p["host_ip"], p["target"]) for p in _published(edge)] == [("127.0.0.1", 8787)]
        assert "/api/_edge/health" in " ".join(edge["healthcheck"]["test"])
        assert edge["depends_on"]["api"]["condition"] == "service_healthy"
        dockerfile = (REPO_ROOT / "deploy" / "edge" / "Dockerfile").read_text()
        for setting in ("WRANGLER_SEND_METRICS=false", "WRANGLER_SEND_ERROR_REPORTS=false",
                        "CLOUDFLARE_CF_FETCH_ENABLED=false"):
            assert setting in dockerfile, setting

    def test_edge_assets_can_be_a_prepared_directory(self, compose_config, tmp_path):
        env = dict(DUMMY_STACK_ENV, ATUS_STACK_ASSETS_DIR=str(tmp_path),
                   ATUS_STACK_SNAPSHOT_ID="abc-123", ATUS_STACK_ANALYTICS_VERSION="0.2",
                   ATUS_EXPECTED_DATA_VERSION="0325:serving-x")
        edge = compose_config(PROD, env=env, profiles=("edge",))["services"]["edge"]
        assert edge["volumes"][0]["source"] == str(tmp_path)
        assert edge["environment"]["SNAPSHOT_ID"] == "abc-123"
        assert edge["environment"]["ANALYTICS_VERSION"] == "0.2"
        assert edge["environment"]["DATA_VERSION"] == "0325:serving-x"

    def test_snapshot_api_is_the_api_with_generous_limits_on_loopback(self, config):
        api, snapshot = config["services"]["api"], config["services"]["snapshot-api"]
        assert snapshot["profiles"] == ["snapshot"]
        assert snapshot["image"] == api["image"]
        extra = {k: v for k, v in snapshot["environment"].items() if k not in api["environment"]}
        assert extra == {"ATUS_API_RATE_ANALYSIS": "100000/60",
                         "ATUS_API_RATE_REQUESTS": "1000000/60"}
        assert all(snapshot["environment"][k] == v for k, v in api["environment"].items())
        assert [(p["host_ip"], p["target"]) for p in _published(snapshot)] == [
            ("127.0.0.1", 10000)]
        assert snapshot["read_only"] is True and snapshot["cap_drop"] == ["ALL"]


# --- emulation override (docker-compose.emulation.yml) ---------------------------


class TestEmulationCompose:
    @pytest.fixture
    def config(self, emulation_config):
        return emulation_config


    def test_api_gets_render_free_limits(self, config):
        api = config["services"]["api"]
        assert api["cpus"] == pytest.approx(0.1)
        assert int(api["mem_limit"]) == 512 * MIB
        assert int(api["memswap_limit"]) == 512 * MIB  # no swap

    def test_db_defaults_to_the_neon_floor(self, config):
        db = config["services"]["db"]
        assert db["cpus"] == pytest.approx(0.25)
        assert int(db["mem_limit"]) == 1024 * MIB
        assert int(db["memswap_limit"]) == 1024 * MIB
        assert _db_settings(db)["jit"] == "off"

    def test_db_size_comes_from_the_environment(self, compose_config):
        env = dict(DUMMY_STACK_ENV, ATUS_EMU_DB_CPUS="2", ATUS_EMU_DB_MEMORY="8g")
        db = compose_config(PROD, EMULATION, env=env)["services"]["db"]
        assert db["cpus"] == pytest.approx(2.0)
        assert int(db["mem_limit"]) == 8 * 1024 * MIB

    def test_pgbouncer_runs_in_transaction_mode_on_loopback(self, config):
        pgbouncer = config["services"]["pgbouncer"]
        assert pgbouncer["profiles"] == ["pgbouncer"]
        env = pgbouncer["environment"]
        assert env["PGBOUNCER_MAX_PREPARED_STATEMENTS"] == "1000"
        assert env["PGBOUNCER_DEFAULT_POOL_SIZE"] == str(
            int(0.9 * _neon_settings_module().neon_settings(0.25, 2.0).max_connections)
        )
        assert env["PGBOUNCER_USERS"].split() == [
            "atus_api:dummy-api", "neondb_owner:dummy-owner", "postgres:dummy-superuser"
        ]
        assert [p["host_ip"] for p in _published(pgbouncer)] == ["127.0.0.1"]
        entrypoint = (REPO_ROOT / "deploy" / "pgbouncer" / "entrypoint.sh").read_text()
        assert ': "${PGBOUNCER_POOL_MODE:=transaction}"' in entrypoint

    def test_api_can_be_routed_through_pgbouncer(self, compose_config):
        env = dict(
            DUMMY_STACK_ENV, ATUS_STACK_API_DB_HOST="pgbouncer", ATUS_STACK_API_DB_HOST_PORT="6432"
        )
        api = compose_config(PROD, EMULATION, env=env, profiles=("pgbouncer",))["services"]["api"]
        assert api["environment"]["ATUS_DATABASE_URL"].endswith("@pgbouncer:6432/atus")
        assert api["depends_on"]["pgbouncer"]["required"] is False

    def test_empty_port_variables_mean_random_loopback_ports(self, compose_config):
        env = dict(DUMMY_STACK_ENV, ATUS_STACK_DB_PORT="", ATUS_STACK_PGBOUNCER_PORT="")
        config = compose_config(PROD, EMULATION, env=env, profiles=("pgbouncer",))
        for name in ("db", "pgbouncer"):
            (port,) = _published(config["services"][name])
            assert port["host_ip"] == "127.0.0.1" and not port.get("published"), name
