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
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.api.app import create_app
from atus_pipeline.cli import cli
from atus_pipeline.config import Settings
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.database.versions import read_data_version
from atus_pipeline.loading.loader import load_all
from atus_pipeline.serving import pgtools
from atus_pipeline.serving.artifact import read_manifest, sha256_file
from atus_pipeline.serving.contract import READ
from atus_pipeline.serving.equivalence import compare_results, run, seed_specs
from atus_pipeline.serving.restore import EXPECTED_ROLE_SETTINGS
from atus_pipeline.serving.secrets_file import API_PASSWORD_KEY, API_URL_KEY, read_env_file

# Fixtures of the opt-in TLS restore (the container tests' own).
from tests.deploy.conftest import docker_daemon, docker_prefix  # noqa: F401
from tests.deploy.test_owner_tls import owner_certificates, tls_only_postgres  # noqa: F401
from tests.integration.conftest import (
    MIGRATIONS_DIR,
    _admin_url,
    create_scratch_database,
    drop_scratch_database,
)
from tests.integration.random_dataset import random_settings

# The restore creates roles and databases and logs in as the roles it creates, which a
# transaction pooler's user list does not know; it runs on the direct endpoint.
pytestmark = [pytest.mark.integration, pytest.mark.direct_connection]

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
    """Built and dumped through the CLI: ``atus build-serving-db --artifact-dir``."""
    full = create_scratch_database(test_db_url, "srvr_full")
    serving = create_scratch_database(test_db_url, "srvr_built")
    try:
        with psycopg.connect(full) as conn:
            load_all(conn, random_settings(tmp_path_factory.mktemp("srvr"), full))
        out = tmp_path_factory.mktemp("artifact")
        report_path = out / "build-report.json"
        result = CliRunner().invoke(cli, [
            "build-serving-db", "--source", full, "--target", serving, "--allow-unvalidated",
            "--artifact-dir", str(out), "--report", str(report_path),
        ], env={"ATUS_LOG_LEVEL": "WARNING"})
        assert result.exit_code == 0, result.output
        assert "postgresql://" not in result.output
        report = SimpleNamespace(**json.loads(report_path.read_text()))
        assert f"Data version:  {report.data_version}" in result.output
        manifest_path = out / f"atus-serving-0325-{report.build_id}.manifest.json"
        yield manifest_path, report, serving
    finally:
        drop_scratch_database(full)
        drop_scratch_database(serving)


def restore(dump: Path, secrets_file: Path, api_role: str, *extra: str,
            owner_url_env: str | None = None):
    env = {k: v for k, v in os.environ.items() if k != "ATUS_OWNER_DATABASE_URL"}
    if owner_url_env is not None:
        env["ATUS_OWNER_DATABASE_URL"] = owner_url_env
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
    # Review r2: a passing rotation says so too, not only a failing one.
    assert "Restore passed." in result.stdout
    assert "Note: the API role's password was changed;" in result.stdout
    assert "update the API's ATUS_DATABASE_URL (Render)" in result.stdout
    new_url = read_env_file(restored["secrets_file"])[API_URL_KEY]
    assert new_url != old_url
    with psycopg.connect(new_url) as conn:
        assert conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0] > 0
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(old_url).close()
    restored["api_url"] = new_url


def test_roles_only_passes_after_the_cumulative_statistics_are_lost(restored, cluster):
    """Review r1 (roles-only-fails-after-neon-suspend): Neon discards the
    cumulative statistics (``pg_stat_user_tables.last_analyze``) at every
    compute suspend; ``pg_stat_reset()`` does the same here. ``--roles-only``
    (the password rotation procedure) then failed its error-severity
    "planner statistics" check, after the rotation had taken effect. The
    check now reads the planner statistics themselves (``pg_stats``,
    catalog data that a restart keeps)."""
    dbname = urlsplit(restored["owner_url"]).path.lstrip("/")
    with psycopg.connect(cluster.url(dbname), autocommit=True) as admin:  # superuser
        admin.execute("SELECT pg_stat_reset()")
        assert admin.execute(
            "SELECT count(*) FROM pg_stat_user_tables WHERE schemaname = 'atus' "
            "AND (last_analyze IS NOT NULL OR last_autoanalyze IS NOT NULL)"
        ).fetchone()[0] == 0
    result = restore(restored["dump"], restored["secrets_file"], cluster.api_role,
                     "--roles-only")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "[PASS] serving: planner statistics" in output
    assert "Restore passed." in output


def test_a_failed_check_after_a_rotation_says_the_new_password_is_in_effect(
        restored, cluster):
    """The checks run after roles.sql: when one fails, the rotation has still
    happened, and the verdict says so (an operator who stops at FAILED would
    otherwise leave the API on the old password)."""
    result = restore(restored["dump"], restored["secrets_file"], cluster.api_role,
                     "--roles-only", "--rotate-api-password", "--size-limit-bytes", "1")
    output = result.stdout + result.stderr
    restored["api_url"] = read_env_file(restored["secrets_file"])[API_URL_KEY]
    assert result.returncode == 1
    assert "[FAIL] restore: metered size under the limit" in output
    assert "Restore FAILED." in output
    assert "the API role's password was changed before the checks ran" in output
    with psycopg.connect(restored["api_url"]) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_roles_only_never_replaces_the_live_password_unasked(restored, cluster, tmp_path):
    """Review r2 (roles-only-silent-api-password-rotation): with a secrets file
    that holds no ATUS_API_DB_PASSWORD, or none at the --secrets-file path
    while ATUS_OWNER_DATABASE_URL is exported, --roles-only generated a new
    password, applied it to the live role and printed "Restore passed." with
    no note: the deployed connection string stopped working, silently. Now it
    refuses and changes nothing; --rotate-api-password does it on purpose."""
    live_url = read_env_file(restored["secrets_file"])[API_URL_KEY]
    owner_only = tmp_path / "owner-only.env"
    owner_only.write_text(f"ATUS_OWNER_DATABASE_URL={restored['owner_url']}\n")
    owner_only.chmod(0o600)
    missing = tmp_path / "empty-home" / ".config" / "atus" / "neon.env"
    for secrets_file, exported in ((owner_only, None), (missing, restored["owner_url"])):
        before = secrets_file.read_text() if secrets_file.exists() else None
        result = restore(restored["dump"], secrets_file, cluster.api_role, "--roles-only",
                         owner_url_env=exported)
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        assert f"holds no {API_PASSWORD_KEY}" in output, output
        assert "--rotate-api-password" in output and "nothing was changed" in output
        assert "Restore passed." not in output and "Traceback" not in output
        assert cluster.owner_password not in output
        assert (secrets_file.read_text() if secrets_file.exists() else None) == before
    with psycopg.connect(live_url) as conn:          # the deployed URL still works
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_a_failing_roles_sql_says_why(restored, cluster, tmp_path):
    """Review r1 (cli-drops-pg-tool-error-reason): only the first line of
    psql's failure reached the operator, never PostgreSQL's reason, which is
    what the first Neon restore needs to see (ADR-011 "Unknowns")."""
    bad = tmp_path / "roles.sql"
    bad.write_text("DO $$ BEGIN RAISE EXCEPTION 'simulated provider refusal'; END $$;\n")
    before = restored["secrets_file"].read_text()
    result = restore(restored["dump"], restored["secrets_file"], cluster.api_role,
                     "--roles-only", "--roles-sql", str(bad))
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "psql failed (exit 3" in output
    assert "simulated provider refusal" in output
    assert restored["secrets_file"].read_text() == before   # nothing written
    assert cluster.owner_password not in output


def test_a_restore_that_failed_after_pg_restore_is_finished_with_roles_only(
        restored, cluster, tmp_path):
    """Review r1 (no-resume-after-partial-restore): roles.sql failed after
    pg_restore had committed. The documented rerun refuses the database (not
    empty), and --roles-only skipped VACUUM (ANALYZE), so its statistics
    checks failed: no documented command finished the restore. Now the
    refusal names --roles-only, which analyzes a database that has no
    planner statistics yet."""
    name, owner_url = cluster.new_owned_database("srvr_partial")
    role = f"{cluster.prefix}_srvr_partial_api"
    secrets_file = tmp_path / "neon.env"
    secrets_file.write_text(f"ATUS_OWNER_DATABASE_URL={owner_url}\n")
    secrets_file.chmod(0o600)
    bad = tmp_path / "roles.sql"
    bad.write_text("DO $$ BEGIN RAISE EXCEPTION 'simulated provider refusal'; END $$;\n")
    try:
        failed = restore(restored["dump"], secrets_file, role, "--roles-sql", str(bad))
        assert failed.returncode != 0
        assert "simulated provider refusal" in failed.stdout + failed.stderr
        rerun = restore(restored["dump"], secrets_file, role)
        assert rerun.returncode != 0
        assert "--roles-only" in rerun.stdout + rerun.stderr     # how to finish it
        finished = restore(restored["dump"], secrets_file, role, "--roles-only")
        output = finished.stdout + finished.stderr
        assert finished.returncode == 0, output
        assert "VACUUM (ANALYZE):" in output
        assert "[PASS] serving: planner statistics" in output
        assert "[PASS] serving: activities stored in activity_code order" in output
        api_url = read_env_file(secrets_file)[API_URL_KEY]
        with psycopg.connect(api_url) as conn:
            assert conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0] > 0
    finally:
        with cluster.admin() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def test_roles_only_refuses_a_database_that_is_not_a_serving_database(
        restored, cluster, tmp_path):
    """``--roles-only`` skips the emptiness check, so it must not apply
    roles.sql (REVOKE ... FROM PUBLIC, a new login role) to a database that
    is not a serving build, such as the full database."""
    name, owner_url = cluster.new_owned_database("srvr_notserving")
    with psycopg.connect(owner_url) as conn:
        apply_migrations(conn, MIGRATIONS_DIR)       # the full database's schema
    secrets_file = tmp_path / "neon.env"
    secrets_file.write_text(f"ATUS_OWNER_DATABASE_URL={owner_url}\n")
    secrets_file.chmod(0o600)
    before = secrets_file.read_text()
    role = f"{cluster.prefix}_srvr_notserving_api"
    try:
        result = restore(restored["dump"], secrets_file, role, "--roles-only")
        assert result.returncode != 0
        assert "no serving marker" in result.stdout + result.stderr
        assert "Traceback" not in result.stdout + result.stderr
        assert secrets_file.read_text() == before    # no API credentials written
        with cluster.admin() as conn:
            assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s",
                                (role,)).fetchone()[0] == 0
        with psycopg.connect(owner_url) as conn:      # PUBLIC's privileges untouched
            assert conn.execute(
                "SELECT has_database_privilege('public', current_database(), 'TEMPORARY')"
            ).fetchone()[0] is True
    finally:
        with cluster.admin() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


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


def test_roles_sql_refuses_a_database_without_the_serving_marker(cluster, tmp_path):
    """Run by hand against the wrong database (the full one, say), roles.sql
    stops before it revokes anything or creates the role."""
    name, owner_url = cluster.new_owned_database("srvr_unmarked")
    role = f"{cluster.prefix}_srvr_unmarked_api"
    try:
        with psycopg.connect(owner_url) as conn:
            apply_migrations(conn, MIGRATIONS_DIR)       # the full database's schema
        tool = pgtools.resolve("psql", owner_url, server_major=16,
                               extra_env={"ATUS_API_ROLE_PASSWORD": secrets.token_urlsafe(24)})
        result = subprocess.run(
            [*tool.argv, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", f"api_role={role}",
             "-f", "-"],
            input=ROLES_SQL.read_bytes(), capture_output=True, env=tool.env, check=False,
        )
        assert result.returncode != 0
        assert b"no serving marker" in result.stderr
        with cluster.admin() as conn:
            assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s",
                                (role,)).fetchone()[0] == 0
        with psycopg.connect(owner_url) as conn:
            assert conn.execute(
                "SELECT has_database_privilege('public', current_database(), 'TEMPORARY')"
            ).fetchone()[0] is True
    finally:
        with cluster.admin() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def test_the_restore_verifies_tls_like_owner_step_3_5(artifact, tmp_path, request,
                                                      monkeypatch):
    """Owner step 3.5 against a TLS-only server with the runbook's DSN
    (``sslmode=verify-full&sslrootcert=system``), the CA in the bundle
    ``SSL_CERT_FILE`` names (the owner's machine; opt-in like the other
    container tests: ``ATUS_DOCKER_TESTS=1``). Where no local client is new
    enough (the owner's Mac), pg_restore and psql run from the postgres image,
    which has no CA bundle of its own."""
    server_url = request.getfixturevalue("tls_only_postgres")   # skips without docker
    certificates = request.getfixturevalue("owner_certificates")
    monkeypatch.setenv("SSL_CERT_FILE", str(certificates / "ca.crt"))
    owner_password = secrets.token_urlsafe(18)
    with psycopg.connect(server_url, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE neondb_owner LOGIN CREATEDB CREATEROLE "
                     f"PASSWORD '{owner_password}'")
        conn.execute("CREATE DATABASE atus OWNER neondb_owner")
    parts = urlsplit(server_url)
    owner_url = urlunsplit(parts._replace(
        netloc=f"neondb_owner:{owner_password}@{parts.hostname}:{parts.port}", path="/atus"))
    manifest_path, _, _ = artifact
    dump = manifest_path.parent / read_manifest(manifest_path)["artifact"]["file"]
    secrets_file = tmp_path / "neon.env"
    secrets_file.write_text(f"ATUS_OWNER_DATABASE_URL='{owner_url}'\n")
    secrets_file.chmod(0o600)
    result = restore(dump, secrets_file, "atus_api")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Restore passed." in output and owner_password not in output
    api_url = read_env_file(secrets_file)[API_URL_KEY]
    assert "sslmode=verify-full" in api_url and "sslrootcert=system" in api_url
