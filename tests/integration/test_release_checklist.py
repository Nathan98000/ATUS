"""The new-ATUS-year checklist, end to end (brief §15; audit F23, F55).

BLS's next multi-year file will cover 2003-2026 under a new release code. This
registers a synthetic ``0326`` release (the only edit a real one needs, plus
its committed lexicon CSV), stages the hand-computed fixture under 0326's file
names with one 2026 respondent added, and runs the pipeline with
``release="0326"``: source validation, the build-and-switch load, the engine,
the database checks and the API. Every release-specific value (file names,
expected years, lexicon file, lexicon text, the data version, ``/meta``) must
follow the registry entry. The frontend takes its year list from ``/meta``.
"""

from __future__ import annotations

import csv
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from atus_pipeline.analytics import (
    ActivitySelector,
    AnalysisEngine,
    AnalysisSpec,
    Measure,
    UnknownActivityError,
)
from atus_pipeline.api.app import create_app
from atus_pipeline.database.versions import read_data_version
from atus_pipeline.loading.loader import load_all
from atus_pipeline.sources import (
    get_source,
    multiyear_release,
    register_release,
    release_files,
    unregister_release,
)
from atus_pipeline.validation.db_checks import run_db_checks
from atus_pipeline.validation.source_checks import validate_all_sources
from tests import fixtures
from tests.integration.analytics_dataset import analytics_settings, record_manifest

pytestmark = pytest.mark.integration

REPO_LEXICON = Path(__file__).resolve().parents[2] / "data" / "reference" / (
    "activity_lexicon_0325.csv"
)
CASE_2026 = "20260101000026"


def _append(settings, key: str, rows: list[dict[str, str]]) -> None:
    path = settings.staging_dir / get_source(settings.release, key).dat_name
    with path.open(newline="") as fh:
        columns = next(csv.reader(fh))
    with path.open("a", newline="") as fh:
        csv.DictWriter(fh, fieldnames=columns).writerows(rows)


def _add_2026_respondent(settings) -> None:
    """One 2026 diary (Wednesday 2026-01-14): 480 minutes of sleep, then TV."""
    _append(settings, "respondent", [fixtures.respondent_row(
        TUCASEID=CASE_2026, TUYEAR="2026", TUDIARYDATE="20260114", TUDIARYDAY="4",
        TUFNWGTP="1.000000", TELFS="1",
    )])
    _append(settings, "roster", [fixtures.roster_row(
        TUCASEID=CASE_2026, TULINENO="1", TERRP="18", TEAGE="45", TESEX="2",
    )])
    episodes = [
        fixtures.activity_row(
            TUCASEID=CASE_2026, TUACTIVITY_N="1", TRCODEP="010101", TRTIER1P="01",
            TRTIER2P="0101", TUSTARTTIM="04:00:00", TUSTOPTIME="12:00:00",
            TUACTDUR24="480", TUACTDUR="480", TUCUMDUR24="480",
        ),
        fixtures.activity_row(
            TUCASEID=CASE_2026, TUACTIVITY_N="2", TRCODEP="120303", TRTIER1P="12",
            TRTIER2P="1203", TUSTARTTIM="12:00:00", TUSTOPTIME="04:00:00",
            TUACTDUR24="960", TUACTDUR="960", TUCUMDUR24="1440",
        ),
    ]
    _append(settings, "activity", episodes)
    _append(settings, "who", [
        fixtures.who_row(TUCASEID=CASE_2026, TUACTIVITY_N=e["TUACTIVITY_N"]) for e in episodes
    ])
    _append(settings, "cps", [fixtures.cps_row(TUCASEID=CASE_2026, TULINENO="1")])
    _append(settings, "replicate_weights", [fixtures.replicate_weights_row(
        TUCASEID=CASE_2026, **{f"TUFNWGTP{i:03d}": "1.000000" for i in range(1, 161)},
    )])


def _staged_rows(settings) -> dict[str, int]:
    counts = {}
    for source in release_files(settings.release):
        path = settings.staging_dir / source.dat_name
        counts[source.key] = sum(1 for _ in path.open()) - 1 if path.exists() else 0
    return counts


@pytest.fixture
def release_0326(tmp_path, scratch_database):
    """Fixture data staged as release 0326, registered with its true counts."""
    url = scratch_database("release")

    def entry(counts):
        return multiyear_release("0326", first_year=2003, last_year=2026, expected_rows=counts)

    # file names first (counts are filled in once the files are staged)
    keys = ("respondent", "roster", "activity", "who", "cps", "replicate_weights",
            "pandemic_weights", "activity_summary")
    register_release(entry(dict.fromkeys(keys, 0)))
    try:
        # no 0326 lexicon is committed: stage 0325's under 0326's file name
        settings = analytics_settings(tmp_path, url, release="0326", lexicon_source=REPO_LEXICON)
        _add_2026_respondent(settings)
        record_manifest(settings.data_dir, "0326")
        counts = _staged_rows(settings)
        unregister_release("0326")
        register_release(entry(counts))
        yield settings
    finally:
        unregister_release("0326")


def test_a_new_release_flows_through_the_pipeline(release_0326):
    settings = release_0326

    # source validation: 0326 file names and counts; years expected 2003-2026
    checks = {r.name: r for r in validate_all_sources(
        release_files("0326"), settings.staging_dir, "0326"
    )}
    assert checks["respondent: row count matches official BLS count"].passed
    years_check = checks["respondent: all survey years present"]
    assert years_check.detail == "expected 2003-2026"  # the fixture holds 4 of them

    with psycopg.connect(settings.database_url) as conn:
        load_all(conn, settings)
        assert read_data_version(conn).startswith("0326:load-")
        assert conn.execute(
            "SELECT release FROM atus.ingestion_runs WHERE status = 'succeeded' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0] == "0326"

        engine = AnalysisEngine(conn)
        sleep = ActivitySelector(("010101",), label="Sleeping")
        result = engine.estimate(AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2026,)))
        assert result.estimate.value == pytest.approx(480.0)
        assert (
            "Activity codes use the BLS-harmonized 2003-26 multi-year lexicon "
            "(cross-year comparable by construction)."
        ) in result.warnings
        trend = engine.trend(
            AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2023, 2026), variance="none")
        )
        assert [p.year for p in trend.points] == [2023, 2026]
        with pytest.raises(UnknownActivityError, match="2003-26 harmonized activity lexicon"):
            engine.estimate(
                AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, ActivitySelector(("010198",)),
                             (2026,))
            )

        db_checks = {r.name: r for r in run_db_checks(conn, settings, persist=False)}
        assert db_checks["lexicon loaded completely: activity_codes"].passed
        assert db_checks["latest survey year"].passed
        assert db_checks["latest survey year"].detail == "expected 2,026 — release 0326"

    with TestClient(create_app(settings)) as client:
        meta = client.get("/api/v1/meta").json()
        assert meta["data"]["release"] == "0326"
        # the SPA builds its year pickers from this list
        assert meta["data"]["years"] == [2019, 2020, 2023, 2026]
        response = client.post(
            "/api/v1/analysis/estimate",
            json={"activity": {"include": ["010101"]}, "years": [2026]},
        )
        assert response.status_code == 200
        assert any("2003-26" in w for w in response.json()["warnings"])
