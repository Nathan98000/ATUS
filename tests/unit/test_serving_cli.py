"""The serving commands' error reporting, without a database: a failing
client tool's reason reaches the operator (review r1,
cli-drops-pg-tool-error-reason), and no secret does."""

from __future__ import annotations

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
