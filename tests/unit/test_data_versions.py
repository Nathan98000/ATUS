"""data_version formatting: explicit versions win; legacy rows stay unique
across rebuilt databases because the run's start time is part of the string."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

from atus_pipeline.database.versions import (
    UNLOADED,
    format_legacy_version,
    format_load_version,
    format_serving_version,
    is_serving_version,
    new_build_id,
    release_of,
    version_from_row,
)

LOAD = re.compile(r"^0325:load-42-\d{8}T\d{6}Z-[0-9a-f]{6}$")
SERVING = re.compile(r"^0325:serving-\d{8}T\d{6}Z-[0-9a-f]{8}:[0-9a-f]{12}$")
NOW = datetime(2026, 10, 20, 17, 5, 9, tzinfo=UTC)


class TestWriterFormats:
    def test_load_version_format(self):
        assert format_load_version("0325", 42, now=NOW, token="a1b2c3") == (
            "0325:load-42-20261020T170509Z-a1b2c3"
        )
        assert LOAD.match(format_load_version("0325", 42))

    def test_load_version_stamp_is_utc(self):
        eastern = NOW.astimezone(timezone(timedelta(hours=-4)))
        assert format_load_version("0325", 42, now=eastern, token="000000") == (
            "0325:load-42-20261020T170509Z-000000"
        )

    def test_two_loads_in_the_same_second_differ(self):
        versions = {format_load_version("0325", 1, now=NOW) for _ in range(50)}
        assert len(versions) == 50

    def test_serving_version_format(self):
        build_id = new_build_id(now=NOW, token="deadbeef")
        assert build_id == "20261020T170509Z-deadbeef"
        version = format_serving_version("0325", build_id, "e8fa76a7c5b8f6e21d0d7f33")
        assert version == "0325:serving-20261020T170509Z-deadbeef:e8fa76a7c5b8"
        assert SERVING.match(format_serving_version("0325", new_build_id(), "ab" * 32))

    def test_serving_versions_are_recognized(self):
        assert is_serving_version("0325:serving-20261020T170509Z-deadbeef:e8fa76a7c5b8")
        assert not is_serving_version("0325:load-42-20261020T170509Z-a1b2c3")
        assert not is_serving_version("0325:run1:1788919495952073")
        assert not is_serving_version(UNLOADED)
        assert not is_serving_version(None)

    def test_every_format_starts_with_its_release(self):
        assert release_of(format_load_version("0326", 1)) == "0326"
        assert release_of(format_serving_version("0325", new_build_id(), "00" * 32)) == "0325"
        assert release_of("0325:run1:1788919495952073") == "0325"
        assert release_of(UNLOADED) is None


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
