"""The release artifact and its restore (brief §9, §19; ADR-011 "Roles").

Builds a serving database from the randomized fixture, dumps it with
``pg_dump -Fc`` into a checksummed artifact, and restores it with
``scripts/ops/restore_serving_db.sh`` into a new database owned by a
non-superuser role that is not named ``atus`` (as Neon's ``neondb_owner``:
LOGIN CREATEDB CREATEROLE). Then, as the API role that ``roles.sql``
created: every API read works and returns what the owner's engine returns,
and every write, DDL statement and temporary table is refused.

Needs ``pg_dump``/``pg_restore``/``psql`` 16+ locally or docker with the
``postgres:16`` image; skipped otherwise.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings
from atus_pipeline.database.versions import read_data_version
from atus_pipeline.loading.loader import load_all
from atus_pipeline.serving import pgtools
from atus_pipeline.serving.artifact import read_manifest, sha256_file, write_artifact
from atus_pipeline.serving.builder import build_serving_database
from atus_pipeline.serving.contract import READ
from atus_pipeline.serving.equivalence import compare_results, run, seed_specs
from atus_pipeline.serving.restore import EXPECTED_ROLE_SETTINGS
from atus_pipeline.serving.secrets_file import API_PASSWORD_KEY, API_URL_KEY, read_env_file
from tests.integration.conftest import (
    _admin_url,
    create_scratch_database,
    drop_scratch_database,
)
from tests.integration.random_dataset import random_settings

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
RESTORE = REPO / "scripts" / "ops" / "restore_serving_db.sh"
ROLES_SQL = REPO / "scripts" / "ops" / "roles.sql"


def _client_tools_available() -> bool:
    if all((pgtools.local_major(t) or 0) >= 16 for t in pgtools.TOOLS):
        return True
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(["docker", "image", "inspect", "postgres:16"],
                           capture_output=True, check=False)
    return probe.returncode == 0


if not _client_tools_available():
    pytest.skip("needs PostgreSQL 16 client tools or docker with postgres:16",
                allow_module_level=True)


class Cluster:
    """Names for the disposable roles and databases of this module."""

    def __init__(self, test_db_url: str):
        self.test_db_url = test_db_url
        self.prefix = urlsplit(test_db_url).path.lstrip("/")
        self.owner = f"{self.prefix}_srvr_owner"
        self.api_role = f"{self.prefix}_srvr_api"
        self.owner_password = secrets.token_urlsafe(18)

    def url(self, dbname: str, user: str | None = None, password: str | None = None) -> str:
        parts = urlsplit(self.test_db_url)
        netloc = (parts.netloc if user is None
                  else f"{user}:{password}@{parts.hostname}:{parts.port}")
        return urlunsplit(parts._replace(netloc=netloc, path=f"/{dbname}"))

    def admin(self):
        return psycopg.connect(_admin_url(self.test_db_url), autocommit=True)

    def new_owned_database(self, suffix: str) -> tuple[str, str]:
        name = f"{self.prefix}_{suffix}"
        with self.admin() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{name}" OWNER "{self.owner}"')
        return name, self.url(name, self.owner, self.owner_password)


@pytest.fixture(scope="module")
def cluster(test_db_url):
    c = Cluster(test_db_url)
    with c.admin() as conn:
        for role in (c.api_role, c.owner):
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')
        # Neon's neondb_owner: LOGIN CREATEDB CREATEROLE, not a superuser.
        conn.execute(f"CREATE ROLE \"{c.owner}\" LOGIN CREATEDB CREATEROLE "
                     f"PASSWORD '{c.owner_password}'")
    yield c
    with c.admin() as conn:
        for (name,) in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s",
                (f"{c.prefix}_srvr_%",)).fetchall():
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        for role in (c.api_role, c.owner):
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


@pytest.fixture(scope="module")
def artifact(test_db_url, tmp_path_factory):
    full = create_scratch_database(test_db_url, "srvr_full")
    serving = create_scratch_database(test_db_url, "srvr_built")
    try:
        with psycopg.connect(full) as conn:
            load_all(conn, random_settings(tmp_path_factory.mktemp("srvr"), full))
        report = build_serving_database(full, serving, require_validated=False)
        out = tmp_path_factory.mktemp("artifact")
        manifest_path = write_artifact(serving, out, report)
        yield manifest_path, report, serving
    finally:
        drop_scratch_database(full)
        drop_scratch_database(serving)


def restore(dump: Path, secrets_file: Path, api_role: str, *extra: str):
    env = {k: v for k, v in os.environ.items() if k != "ATUS_OWNER_DATABASE_URL"}
    env["ATUS_BIN"] = str(Path(os.environ.get("VIRTUAL_ENV", REPO / ".venv")) / "bin" / "atus")
    if not Path(env["ATUS_BIN"]).exists():
        env["ATUS_BIN"] = shutil.which("atus") or "atus"
    return subprocess.run(
        [str(RESTORE), "--artifact", str(dump), "--secrets-file", str(secrets_file),
         "--api-role", api_role, "--size-limit-bytes", "0", *extra],
        capture_output=True, text=True, env=env, check=False,
    )


@pytest.fixture(scope="module")
def restored(cluster, artifact, tmp_path_factory):
    manifest_path, report, _ = artifact
    manifest = read_manifest(manifest_path)
    dump = manifest_path.parent / manifest["artifact"]["file"]
    _, owner_url = cluster.new_owned_database("srvr_restored")
    secrets_dir = tmp_path_factory.mktemp("secrets")
    secrets_file = secrets_dir / "neon.env"
    secrets_file.write_text(f"ATUS_OWNER_DATABASE_URL={owner_url}\n")
    secrets_file.chmod(0o600)
    report_file = secrets_dir / "restore-report.json"
    result = restore(dump, secrets_file, cluster.api_role, "--verify-content-hash",
                     "--report", str(report_file))
    assert result.returncode == 0, result.stdout + result.stderr
    api_url = read_env_file(secrets_file)[API_URL_KEY]
    return {"owner_url": owner_url, "api_url": api_url, "secrets_file": secrets_file,
            "output": result.stdout + result.stderr, "report": report_file,
            "manifest": manifest, "dump": dump}


def test_the_artifact_is_checksummed_and_described(artifact):
    manifest_path, report, _ = artifact
    manifest = read_manifest(manifest_path)
    dump = manifest_path.parent / manifest["artifact"]["file"]
    assert dump.name == f"atus-serving-0325-{report.build_id}.dump"
    assert dump.read_bytes()[:5] == b"PGDMP"                  # pg_dump custom format
    digest = sha256_file(dump)
    assert manifest["artifact"]["sha256"] == digest
    assert (dump.parent / f"{dump.name}.sha256").read_text() == f"{digest}  {dump.name}\n"
    assert manifest["data_version"] == report.data_version
    assert manifest["content_hash"] == report.content_hash
    assert manifest["source"]["data_version"].startswith("0325:load-")
    assert manifest["tables"]["activities"]["rows"] == report.tables["activities"]["rows"]
    assert manifest["counts"]["replicate_weights.omitted"] == 60
    assert manifest["tools"]["pg_dump"].startswith("pg_dump (PostgreSQL) 16")
    assert manifest["artifact"]["under_github_release_asset_limit"] is True
    text = manifest_path.read_text()
    assert "postgresql://" not in text and "password" not in text.lower()


def test_the_restore_reproduces_the_build_exactly(restored, artifact):
    _, report, _ = artifact
    assert "Restore passed." in restored["output"]
    assert "[PASS] serving: content hash" in restored["output"]
    with psycopg.connect(restored["owner_url"]) as conn:
        assert read_data_version(conn) == report.data_version
        owner = conn.execute("SELECT DISTINCT tableowner FROM pg_tables "
                             "WHERE schemaname = 'atus'").fetchall()
        assert owner == [(urlsplit(restored["owner_url"]).username,)]
        assert conn.execute(
            "SELECT correlation FROM pg_stats WHERE schemaname = 'atus' "
            "AND tablename = 'activities' AND attname = 'activity_code'"
        ).fetchone()[0] == pytest.approx(1.0)


def test_no_secret_reaches_the_output_or_the_report(restored, cluster):
    password = read_env_file(restored["secrets_file"])[API_PASSWORD_KEY]
    report_text = restored["report"].read_text()
    for secret in (cluster.owner_password, password, restored["api_url"]):
        assert secret not in restored["output"]
        assert secret not in report_text
    assert stat.S_IMODE(restored["secrets_file"].stat().st_mode) == 0o600
    assert len(password) >= 32
    report = json.loads(report_text)
    assert report["passed"] is True and report["owner_is_superuser"] is False


def test_every_api_read_works_as_the_api_role(restored, tmp_path):
    api_url, owner_url = restored["api_url"], restored["owner_url"]
    with psycopg.connect(owner_url) as owner, psycopg.connect(api_url) as api:
        years = [y for (y,) in owner.execute(
            "SELECT DISTINCT data_year FROM atus.respondents ORDER BY 1")]
        tier1 = [c for (c,) in owner.execute("SELECT code FROM atus.activity_tier1")]
        specs = seed_specs(years, tier1)
        for op, spec in specs:
            a = run(AnalysisEngine(owner), op, spec)
            b = run(AnalysisEngine(api), op, spec)
            assert compare_results(a, b).equivalent, (op, spec)
    settings = Settings(database_url=api_url, data_dir=tmp_path, release="0325",
                        log_level="WARNING")
    analysis = {"activity": {"include": ["010101"]}, "years": [2023]}
    with TestClient(create_app(settings)) as client:
        for path in ("/health", "/api/v1/meta", "/api/v1/activities",
                     "/api/v1/activities/presets", "/api/v1/activities/01",
                     "/api/v1/activities/010101", "/api/v1/population/metadata"):
            assert client.get(path).status_code == 200, path
        for path, body in (
            ("estimate", analysis),
            ("trend", {**analysis, "years": [2019, 2021, 2023]}),
            ("compare", {**analysis, "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}),
            ("estimate", {**analysis, "weights": "pandemic", "years": [2019, 2020]}),
        ):
            response = client.post(f"/api/v1/analysis/{path}", json=body)
            assert response.status_code == 200, (path, response.text)


WRITES = [
    *(f"INSERT INTO atus.{t} OVERRIDING SYSTEM VALUE SELECT * FROM atus.{t} LIMIT 1"
      for t in READ),
    *(f"UPDATE atus.{t} SET {READ[t][-1]} = {READ[t][-1]} WHERE false" for t in READ),
    *(f"DELETE FROM atus.{t} WHERE false" for t in READ),
    *(f"TRUNCATE atus.{t}" for t in READ),
    "INSERT INTO atus.validation_results (check_name, severity, passed) "
    "VALUES ('x', 'error', true)",
    "SELECT * FROM atus.respondents LIMIT 1 FOR UPDATE",
    "CREATE TABLE atus.probe (x int)",
    "CREATE TABLE public.probe (x int)",
    "CREATE TEMP TABLE probe (x int)",
    "CREATE SCHEMA probe",
    "CREATE INDEX probe ON atus.activities (duration_minutes)",
    "CREATE VIEW atus.probe AS SELECT 1",
    "CREATE FUNCTION public.probe() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    "DROP TABLE atus.activity_tier1",
    "ALTER TABLE atus.respondents ADD COLUMN probe int",
    "COMMENT ON TABLE atus.respondents IS 'probe'",
    "LOCK TABLE atus.activities IN ACCESS EXCLUSIVE MODE",
    "SELECT nextval(pg_get_serial_sequence('atus.ingestion_runs', 'id'))",
    "SELECT * FROM atus.validation_results",
    "SELECT * FROM atus.source_files",
    "SELECT * FROM public.schema_migrations",
]


@pytest.mark.parametrize("statement", WRITES)
def test_the_api_role_cannot_write_or_create(restored, statement):
    """Privileges, not the read-only default, refuse each statement: it runs
    in an explicit READ WRITE transaction."""
    with psycopg.connect(restored["api_url"], autocommit=True) as conn:
        conn.read_only = False
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction(force_rollback=True):
                conn.execute(statement)


def test_the_api_role_cannot_pass_its_privileges_on(restored):
    with psycopg.connect(restored["api_url"], autocommit=True) as conn:
        conn.read_only = False
        with conn.transaction():          # READ WRITE: a warning, and no effect
            conn.execute("GRANT SELECT ON atus.respondents TO PUBLIC")
    with psycopg.connect(restored["owner_url"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE grantee = 'PUBLIC' AND table_schema = 'atus'"
        ).fetchone()[0] == 0


def test_the_api_role_defaults_bound_every_session(restored):
    with psycopg.connect(restored["api_url"]) as conn:
        settings = {name: conn.execute(f"SHOW {name}").fetchone()[0]
                    for name in EXPECTED_ROLE_SETTINGS}
        assert settings == EXPECTED_ROLE_SETTINGS
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TEMP TABLE probe (x int)")
        conn.rollback()
        attributes = conn.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
            "rolconnlimit FROM pg_roles WHERE rolname = current_user").fetchone()
        assert attributes == (False, False, False, False, False, 10)


def test_a_restored_database_is_never_restored_over(restored, cluster):
    result = restore(restored["dump"], restored["secrets_file"], cluster.api_role)
    assert result.returncode != 0
    assert "not empty" in result.stdout + result.stderr


def test_a_changed_artifact_is_refused_before_anything_is_restored(restored, cluster, tmp_path):
    manifest_name = restored["dump"].name.replace(".dump", ".manifest.json")
    copy = tmp_path / restored["dump"].name
    data = bytearray(restored["dump"].read_bytes())
    data[len(data) // 2] ^= 0xFF
    copy.write_bytes(bytes(data))
    shutil.copy(restored["dump"].parent / manifest_name, tmp_path / manifest_name)
    shutil.copy(restored["dump"].parent / f"{restored['dump'].name}.sha256", tmp_path)
    name, owner_url = cluster.new_owned_database("srvr_tampered")
    secrets_file = tmp_path / "neon.env"
    secrets_file.write_text(f"ATUS_OWNER_DATABASE_URL={owner_url}\n")
    result = restore(copy, secrets_file, cluster.api_role)
    assert result.returncode != 0
    assert "SHA-256 does not match" in result.stdout + result.stderr
    with psycopg.connect(owner_url) as conn:
        assert conn.execute("SELECT to_regnamespace('atus')").fetchone()[0] is None


def test_roles_only_rotates_the_api_password(restored, cluster):
    old_url = restored["api_url"]
    result = restore(restored["dump"], restored["secrets_file"], cluster.api_role,
                     "--roles-only", "--rotate-api-password")
    assert result.returncode == 0, result.stdout + result.stderr
    new_url = read_env_file(restored["secrets_file"])[API_URL_KEY]
    assert new_url != old_url
    with psycopg.connect(new_url) as conn:
        assert conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0] > 0
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(old_url).close()
    restored["api_url"] = new_url


def test_roles_sql_refuses_to_run_without_a_password(cluster, restored):
    tool = pgtools.resolve("psql", restored["owner_url"], server_major=16)
    env = {k: v for k, v in tool.env.items() if k != "ATUS_API_ROLE_PASSWORD"}
    result = subprocess.run(
        [*tool.argv, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v",
         f"api_role={cluster.prefix}_srvr_nopw", "-f", "-"],
        input=ROLES_SQL.read_bytes(), capture_output=True, env=env, check=False,
    )
    assert result.returncode != 0
    assert b"no API password" in result.stderr
    with cluster.admin() as conn:
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s",
                            (f"{cluster.prefix}_srvr_nopw",)).fetchone()[0] == 0
