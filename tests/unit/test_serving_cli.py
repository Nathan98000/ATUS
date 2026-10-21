"""The serving commands without a database: a failing client tool's reason
reaches the operator (review r1, cli-drops-pg-tool-error-reason) and no
secret does; the restore refuses an owner connection string from the
environment that names another database than its secrets file (review r1,
owner-url-env-overrides-secrets-file)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from atus_pipeline.cli import cli
from atus_pipeline.serving import restore as restore_module
from atus_pipeline.serving.pgtools import PgToolError

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
