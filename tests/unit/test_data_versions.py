"""data_version formatting: explicit versions win; legacy rows stay unique
across rebuilt databases because the run's start time is part of the string."""

from __future__ import annotations

from datetime import UTC, datetime

from atus_pipeline.database.versions import UNLOADED, format_legacy_version, version_from_row


def test_explicit_data_version_is_used_verbatim():
    row = {"id": 1, "release": "0325", "data_version": "0325:serving-abc:0123456789ab"}
    assert version_from_row(row) == "0325:serving-abc:0123456789ab"


def test_no_succeeded_run_is_unloaded():
    assert version_from_row(None) == UNLOADED


def test_legacy_rows_differ_when_a_rebuilt_database_restarts_at_run1():
    first = {"id": 1, "release": "0325", "started_at": "2026-09-09T02:04:55.952073+00:00"}
    rebuilt = {"id": 1, "release": "0325", "started_at": "2026-10-20T17:00:00.000001+00:00"}
    assert version_from_row(first) != version_from_row(rebuilt)
    assert version_from_row(first) == "0325:run1:1788919495952073"


def test_legacy_format_accepts_datetimes():
    started = datetime(2026, 9, 9, 2, 4, 55, 952073, tzinfo=UTC)
    assert format_legacy_version("0325", 1, started) == "0325:run1:1788919495952073"
