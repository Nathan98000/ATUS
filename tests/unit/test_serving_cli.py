"""The serving commands without a database: a failing client tool's reason
reaches the operator (review r1, cli-drops-pg-tool-error-reason) and no
secret does; the restore refuses an owner connection string from the
environment that names another database than its secrets file (review r1,
owner-url-env-overrides-secrets-file)."""

from __future__ import annotations

import psycopg
import pytest
from click.testing import CliRunner
from psycopg.conninfo import conninfo_to_dict

from atus_pipeline.cli import cli
from atus_pipeline.serving import restore as restore_module
from atus_pipeline.serving.pgtools import PgToolError
from atus_pipeline.serving.secrets_file import verified_owner_url

OWNER_URL = "postgresql://neondb_owner:s3cret-owner-pw@db.example.test:5432/atus"


def _run_restore(tmp_path, error: Exception, monkeypatch):
    dump = tmp_path / "atus-serving-0325-x.dump"
    dump.write_bytes(b"PGDMP")

    def failing_restore(options, echo=print):
        raise error

    monkeypatch.setattr(restore_module, "restore", failing_restore)
    return CliRunner().invoke(cli, [
        "restore-serving-db", "--artifact", str(dump), "--roles-only",
        "--secrets-file", str(tmp_path / "neon.env"),
    ], env={"ATUS_OWNER_DATABASE_URL": OWNER_URL, "ATUS_LOG_LEVEL": "WARNING"})


def test_a_failing_tool_reports_its_reason(tmp_path, monkeypatch):
    """Before, only "psql failed (exit 3, docker postgres:16):" was printed."""
    error = PgToolError(
        "psql failed (exit 3, docker postgres:16):\n"
        "psql:<stdin>:57: ERROR:  permission denied to set parameter \"statement_timeout\"\n"
        "CONTEXT:  SQL statement \"ALTER ROLE atus_api SET statement_timeout = '85s'\""
    )
    result = _run_restore(tmp_path, error, monkeypatch)
    assert result.exit_code == 1
    assert "psql failed (exit 3, docker postgres:16):" in result.output
    assert 'permission denied to set parameter "statement_timeout"' in result.output
    assert "ALTER ROLE atus_api SET statement_timeout" in result.output


def test_a_long_tool_error_keeps_its_last_lines_and_no_secret(tmp_path, monkeypatch):
    lines = [f"pg_restore: error: line {i}" for i in range(100)]
    error = PgToolError("pg_restore failed (exit 1, local /usr/bin/pg_restore (16)):\n"
                        + "\n".join(lines) + "\nconnection to " + OWNER_URL + " failed")
    result = _run_restore(tmp_path, error, monkeypatch)
    assert result.exit_code == 1
    assert "pg_restore failed (exit 1" in result.output
    assert "line 99" in result.output and "line 10\n" not in result.output
    assert "s3cret-owner-pw" not in result.output


def test_other_errors_still_show_their_first_line_only(tmp_path, monkeypatch):
    result = _run_restore(
        tmp_path, restore_module.RestoreError("refused\nsecond line"), monkeypatch)
    assert result.exit_code == 1
    assert "refused" in result.output and "second line" not in result.output


# --------------------------------------------------------------------------- #
# which owner connection string the restore uses                              #
# --------------------------------------------------------------------------- #

PROJECT_A = "postgresql://neondb_owner:pw-a@ep-a.example.test:5432/atus?sslmode=verify-full"
PROJECT_B = "postgresql://neondb_owner:pw-b@ep-b.example.test:5432/atus?sslmode=verify-full"


def _secrets_file(tmp_path, owner_url: str | None):
    path = tmp_path / "neon-b.env"
    path.write_text(f"ATUS_OWNER_DATABASE_URL={owner_url}\n" if owner_url else "# empty\n")
    path.chmod(0o600)
    return path


def _options(path):
    return restore_module.RestoreOptions(artifact=path.with_suffix(".dump"), secrets_file=path)


def test_an_exported_owner_url_for_another_database_is_refused(tmp_path, monkeypatch):
    """A stale exported ATUS_OWNER_DATABASE_URL (project A) silently won over
    --secrets-file (project B): the restore rotated A's API password and
    wrote A's DSN into B's file."""
    path = _secrets_file(tmp_path, PROJECT_B)
    monkeypatch.setenv("ATUS_OWNER_DATABASE_URL", PROJECT_A)
    with pytest.raises(restore_module.RestoreError) as info:
        restore_module.owner_url(_options(path))
    message = str(info.value)
    assert "ep-a.example.test" in message and "ep-b.example.test" in message
    assert "pw-a" not in message and "pw-b" not in message


def test_the_cli_refuses_before_touching_anything(tmp_path, monkeypatch):
    path = _secrets_file(tmp_path, PROJECT_B)
    dump = tmp_path / "atus-serving-0325-x.dump"
    dump.write_bytes(b"PGDMP")
    called = []
    monkeypatch.setattr(restore_module, "restore", lambda *a, **k: called.append(a))
    result = CliRunner().invoke(cli, [
        "restore-serving-db", "--artifact", str(dump), "--roles-only",
        "--rotate-api-password", "--secrets-file", str(path),
    ], env={"ATUS_OWNER_DATABASE_URL": PROJECT_A, "ATUS_LOG_LEVEL": "WARNING"})
    assert result.exit_code == 1
    assert "ATUS_OWNER_DATABASE_URL" in result.output
    assert called == []
    assert path.read_text() == f"ATUS_OWNER_DATABASE_URL={PROJECT_B}\n"


@pytest.mark.parametrize("in_file", [None, PROJECT_A.replace("pw-a", "rotated-owner-pw")])
def test_the_environment_still_wins_when_it_names_the_same_database(
        tmp_path, monkeypatch, in_file):
    path = _secrets_file(tmp_path, in_file)
    monkeypatch.setenv("ATUS_OWNER_DATABASE_URL", PROJECT_A)
    assert restore_module.owner_url(_options(path)) == PROJECT_A


def test_the_file_is_used_without_the_environment(tmp_path, monkeypatch):
    path = _secrets_file(tmp_path, PROJECT_B)
    monkeypatch.delenv("ATUS_OWNER_DATABASE_URL", raising=False)
    assert restore_module.owner_url(_options(path)) == PROJECT_B


# --------------------------------------------------------------------------- #
# the owner's connection verifies the server (review r3)                      #
# --------------------------------------------------------------------------- #

NEON_PASTED = ("postgresql://neondb_owner:pw-n@ep-cool-1.us-east-1.aws.neon.tech/atus"
               "?sslmode=require&channel_binding=require")


@pytest.mark.parametrize("from_env", [True, False])
@pytest.mark.parametrize("line", [f"ATUS_OWNER_DATABASE_URL={NEON_PASTED}", NEON_PASTED])
def test_a_pasted_neon_string_is_verified(tmp_path, monkeypatch, from_env, line):
    """Review r3 (owner-restore-unverified-tls): owner step 3.5.1 pastes Neon's
    string as is (``sslmode=require``), and the restore used it unchanged, so
    the owner's session, which streams every served row and sends the API
    role's SCRAM verifier, authenticated no server. Now a remote owner URL
    always verifies the certificate and host name, as the API's URL does;
    channel binding is kept."""
    path = tmp_path / "neon.env"
    path.write_text("# empty\n" if from_env else line + "\n")
    path.chmod(0o600)
    if from_env:
        monkeypatch.setenv("ATUS_OWNER_DATABASE_URL", NEON_PASTED)
    else:
        monkeypatch.delenv("ATUS_OWNER_DATABASE_URL", raising=False)
    params = conninfo_to_dict(restore_module.owner_url(_options(path)))
    assert params["sslmode"] == "verify-full"
    assert params["sslrootcert"] == "system"
    assert params["channel_binding"] == "require"
    assert (params["host"], params["user"], params["password"], params["dbname"]) == (
        "ep-cool-1.us-east-1.aws.neon.tech", "neondb_owner", "pw-n", "atus")


@pytest.mark.parametrize(("given", "expected"), [
    ("postgresql://o:p@db.example.test/atus", {"sslmode": "verify-full",
                                               "sslrootcert": "system"}),
    ("postgresql://o:p@db.example.test/atus?sslmode=disable",
     {"sslmode": "verify-full", "sslrootcert": "system"}),
    ("postgresql://o:p@db.example.test/atus?sslmode=verify-ca&sslrootcert=/ca.pem",
     {"sslmode": "verify-full", "sslrootcert": "/ca.pem"}),
    ("host=db.example.test user=o password='p q' dbname=atus sslmode=prefer",
     {"sslmode": "verify-full", "sslrootcert": "system", "password": "p q"}),
    ("postgresql://o:p@db.example.test/atus?sslmode=require&hostaddr=192.0.2.7",
     {"sslmode": "verify-full", "sslrootcert": "system", "hostaddr": "192.0.2.7"}),
])
def test_every_weaker_sslmode_becomes_verify_full(given, expected):
    params = conninfo_to_dict(verified_owner_url(given))
    assert {key: params.get(key) for key in expected} == expected
    assert params["host"] == "db.example.test" and params["dbname"] == "atus"


@pytest.mark.parametrize("given", [
    PROJECT_A,                                           # already verify-full
    "postgresql://o:p@db.example.test/atus?sslmode=verify-full&sslrootcert=/ca.pem",
    "postgresql://neondb_owner:p@127.0.0.1:5470/atus",   # the drill: the owner's choice
    "postgresql://neondb_owner:p@localhost:5470/atus?sslmode=disable",
    "postgresql://neondb_owner:p@[::1]:5470/atus",
    "host=/tmp user=o dbname=atus",                       # a Unix socket
])
def test_verified_and_local_owner_urls_are_kept(given):
    assert verified_owner_url(given) == given


# --------------------------------------------------------------------------- #
# a new API password is announced, whatever the verdict                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("passed", [True, False])
@pytest.mark.parametrize("changed", [True, False])
def test_a_new_api_password_is_announced_whatever_the_verdict(tmp_path, monkeypatch,
                                                              passed, changed):
    """Review r2 (roles-only-silent-api-password-rotation): the note that the
    API role's password changed was printed only when a check failed, so a
    passing run that set a new password said nothing, and the API's
    ATUS_DATABASE_URL silently stopped opening connections."""
    dump = tmp_path / "atus-serving-0325-x.dump"
    dump.write_bytes(b"PGDMP")
    secrets_file = tmp_path / "neon.env"
    monkeypatch.setattr(restore_module, "restore", lambda options, echo=print: (
        restore_module.RestoreReport(passed=passed, api_password_changed=changed)))
    result = CliRunner().invoke(cli, [
        "restore-serving-db", "--artifact", str(dump), "--roles-only",
        "--secrets-file", str(secrets_file),
    ], env={"ATUS_OWNER_DATABASE_URL": OWNER_URL, "ATUS_LOG_LEVEL": "WARNING"})
    assert result.exit_code == (0 if passed else 1)
    assert ("Restore passed." if passed else "Restore FAILED.") in result.output
    note = "the API role's password was changed"
    assert (note in result.output) is changed
    if changed:
        assert f"only ATUS_API_DATABASE_URL in {secrets_file} works now" in result.output
        assert "update the API's ATUS_DATABASE_URL (Render)" in result.output
        assert ("before the checks ran" in result.output) is not passed


@pytest.mark.parametrize("rotated", [True, False])
@pytest.mark.parametrize("stored", [None, "old-api-pw"], ids=["no file", "stored password"])
def test_a_new_api_password_is_announced_when_a_later_step_fails(tmp_path, monkeypatch,
                                                                 rotated, stored):
    """Review r2 verification (roles-only-silent-api-password-rotation): the
    secrets file is rewritten right after roles.sql, before the VACUUM, the
    checks and the API role's own connection. When one of those raised (a
    dropped connection, say), the CLI printed only the error: a new password
    in effect went unannounced, against the "announce every new one" fix."""
    dump = tmp_path / "atus-serving-0325-x.dump"
    dump.write_bytes(b"PGDMP")
    secrets_file = tmp_path / "neon.env"
    if stored is not None:
        secrets_file.write_text(f"ATUS_API_DB_PASSWORD={stored}\n")
        secrets_file.chmod(0o600)

    def restore_that_stops(options, echo=print):
        if rotated:   # what restore() does once roles.sql succeeded
            secrets_file.write_text("ATUS_API_DB_PASSWORD=new-api-pw\n")
        raise psycopg.OperationalError("connection to server was lost")

    monkeypatch.setattr(restore_module, "restore", restore_that_stops)
    result = CliRunner().invoke(cli, [
        "restore-serving-db", "--artifact", str(dump), "--roles-only",
        "--secrets-file", str(secrets_file),
    ], env={"ATUS_OWNER_DATABASE_URL": OWNER_URL, "ATUS_LOG_LEVEL": "WARNING"})
    assert result.exit_code == 1
    assert "connection to server was lost" in result.output
    note = "Note: the API role's password was changed before the restore stopped with an error"
    assert (note in result.output) is rotated
    if rotated:
        assert f"only ATUS_API_DATABASE_URL in {secrets_file} works now" in result.output
    assert "new-api-pw" not in result.output and "s3cret-owner-pw" not in result.output


# --------------------------------------------------------------------------- #
# a release dump without its manifest                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("extra", [[], ["--roles-only"], ["--roles-only", "--rotate-api-password"]])
@pytest.mark.parametrize("manifest", [None, "{corrupt"], ids=["missing", "corrupt"])
def test_a_dump_without_a_readable_manifest_is_a_clean_error(tmp_path, extra, manifest):
    """Review r2 (restore-missing-manifest-traceback): every mode reads the
    manifest beside the .dump first; a missing or corrupt one ended in a
    FileNotFoundError or JSONDecodeError traceback instead of an error."""
    dump = tmp_path / "atus-serving-0325-x.dump"
    dump.write_bytes(b"PGDMP")
    if manifest is not None:
        (tmp_path / "atus-serving-0325-x.manifest.json").write_text(manifest)
    result = CliRunner().invoke(cli, [
        "restore-serving-db", "--artifact", str(dump), *extra,
        "--secrets-file", str(tmp_path / "neon.env"),
    ], env={"ATUS_OWNER_DATABASE_URL": OWNER_URL, "ATUS_LOG_LEVEL": "WARNING"})
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "atus-serving-0325-x.manifest.json" in result.output
    assert "keep the release's .manifest.json beside" in result.output
    assert "s3cret-owner-pw" not in result.output
    assert not (tmp_path / "neon.env").exists()            # nothing written


def test_verify_serving_db_reports_a_missing_manifest_cleanly(tmp_path):
    result = CliRunner().invoke(cli, [
        "verify-serving-db", "--database-url", OWNER_URL,
        "--manifest", str(tmp_path / "nope.manifest.json"),
    ], env={"ATUS_LOG_LEVEL": "WARNING"})
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "nope.manifest.json" in result.output and "s3cret-owner-pw" not in result.output
