"""End-to-end integration test: staged CSVs -> transform -> load -> query.

A small, internally consistent synthetic dataset (three respondents covering
the ordinary case, the 2020 pandemic case, and the 2019 pandemic-weight case)
is written in the exact source-file layouts, loaded through the real loader,
and verified in the database — including the loader's all-or-nothing
transaction behaviour when a load fails midway.
"""

from __future__ import annotations

import csv
import shutil
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from atus_pipeline.config import Settings
from atus_pipeline.loading.loader import load_all
from atus_pipeline.manifest import Manifest
from atus_pipeline.sources import (
    ACTIVITY_COLUMNS,
    CPS_REQUIRED_COLUMNS,
    PANDEMIC_WEIGHT_COLUMNS,
    REPLICATE_WEIGHT_COLUMNS,
    RESPONDENT_COLUMNS,
    ROSTER_COLUMNS,
    WHO_COLUMNS,
    release_files,
)
from tests import fixtures

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

CASE_A = "20230101230001"   # ordinary 2023 respondent
CASE_B = "20200101200001"   # 2020: no TUFNWGTP, pandemic weight instead
CASE_C = "20190101190001"   # 2019: both weights defined


def _write(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def _build_fixture_dataset(data_dir: Path) -> None:
    staging = data_dir / "staging" / "0325"
    staging.mkdir(parents=True)
    reference = data_dir / "reference"
    reference.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "data" / "reference" / "activity_lexicon_0325.csv",
        reference / "activity_lexicon_0325.csv",
    )

    _write(staging / "atusresp_0325.dat", RESPONDENT_COLUMNS, [
        fixtures.respondent_row(
            TUCASEID=CASE_A, TRERNWA="66000", TEHRUSLT="-4",
            TRSPPRES="1", TRNUMHOU="2",
        ),
        fixtures.respondent_row(
            TUCASEID=CASE_B, TUYEAR="2020", TUDIARYDATE="20200126",
            TUFNWGTP="-1.000000", TU20FWGT="5541150.024906", TELFS="5",
        ),
        fixtures.respondent_row(
            TUCASEID=CASE_C, TUYEAR="2019", TUDIARYDATE="20190127",
            TUFNWGTP="2286291.439799", TU20FWGT="2041308.279500", TELFS="4",
        ),
    ])

    _write(staging / "atusrost_0325.dat", ROSTER_COLUMNS, [
        fixtures.roster_row(TUCASEID=CASE_A, TULINENO="1", TERRP="18", TEAGE="40", TESEX="2"),
        fixtures.roster_row(TUCASEID=CASE_A, TULINENO="2", TERRP="20", TEAGE="42", TESEX="1"),
        fixtures.roster_row(TUCASEID=CASE_B, TULINENO="1", TERRP="19", TEAGE="70", TESEX="1"),
        fixtures.roster_row(TUCASEID=CASE_C, TULINENO="1", TERRP="18", TEAGE="30", TESEX="2"),
    ])

    _write(staging / "atusact_0325.dat", ACTIVITY_COLUMNS, [
        fixtures.activity_row(
            TUCASEID=CASE_A, TUACTIVITY_N="1", TRCODEP="010101", TRTIER1P="01",
            TRTIER2P="0101", TUSTARTTIM="04:00:00", TUSTOPTIME="08:00:00",
            TUACTDUR24="240", TUACTDUR="240", TUCUMDUR24="240",
        ),
        fixtures.activity_row(
            TUCASEID=CASE_A, TUACTIVITY_N="2", TRCODEP="050101", TRTIER1P="05",
            TRTIER2P="0501", TUSTARTTIM="08:00:00", TUSTOPTIME="16:00:00",
            TUACTDUR24="480", TUACTDUR="480", TUCUMDUR24="720", TEWHERE="2",
        ),
        fixtures.activity_row(
            TUCASEID=CASE_A, TUACTIVITY_N="3", TRCODEP="120303", TRTIER1P="12",
            TRTIER2P="1203", TUSTARTTIM="16:00:00", TUSTOPTIME="04:00:00",
            TUACTDUR24="720", TUACTDUR="720", TUCUMDUR24="1440", TEWHERE="1",
        ),
        fixtures.activity_row(
            TUCASEID=CASE_B, TUACTIVITY_N="1", TUSTOPTIME="04:00:00",
            TUACTDUR24="1440", TUACTDUR="1440", TUCUMDUR24="1440",
        ),
        fixtures.activity_row(
            TUCASEID=CASE_C, TUACTIVITY_N="1", TUSTOPTIME="04:00:00",
            TUACTDUR24="1440", TUACTDUR="1440", TUCUMDUR24="1440",
        ),
    ])

    _write(staging / "atuswho_0325.dat", WHO_COLUMNS, [
        fixtures.who_row(TUCASEID=CASE_A, TUACTIVITY_N="1"),  # sleep: not asked
        fixtures.who_row(TUCASEID=CASE_A, TUACTIVITY_N="2", TRWHONA="0", TUWHO_CODE="61"),
        fixtures.who_row(TUCASEID=CASE_A, TUACTIVITY_N="3", TRWHONA="0",
                         TULINENO="2", TUWHO_CODE="20"),
        fixtures.who_row(TUCASEID=CASE_B, TUACTIVITY_N="1"),
        fixtures.who_row(TUCASEID=CASE_C, TUACTIVITY_N="1"),
    ])

    _write(staging / "atuscps_0325.dat", CPS_REQUIRED_COLUMNS, [
        fixtures.cps_row(TUCASEID=CASE_A, TULINENO="1"),
        fixtures.cps_row(TUCASEID=CASE_A, TULINENO="2", PESEX="1", PRTAGE="42"),
        fixtures.cps_row(TUCASEID=CASE_B, TULINENO="1", PRTAGE="70", GESTFIPS="36"),
        fixtures.cps_row(TUCASEID=CASE_C, TULINENO="1", PRTAGE="30"),
        # a household of a nonrespondent: must be filtered out by the loader
        fixtures.cps_row(TUCASEID="20230101299999", TULINENO="1"),
    ])

    _write(staging / "atuswgts_0325.dat", REPLICATE_WEIGHT_COLUMNS, [
        fixtures.replicate_weights_row(TUCASEID=CASE_A),
        fixtures.replicate_weights_row(
            TUCASEID=CASE_B, **{f"TUFNWGTP{i:03d}": "-1.000000" for i in range(1, 161)},
        ),
        fixtures.replicate_weights_row(TUCASEID=CASE_C),
    ])

    _write(staging / "atuswgtspan_1920.dat", PANDEMIC_WEIGHT_COLUMNS, [
        fixtures.pandemic_weights_row(TUCASEID=CASE_B),
        fixtures.pandemic_weights_row(TUCASEID=CASE_C),
    ])

    manifest = Manifest(data_dir / "manifest.json")
    for source in release_files("0325"):
        manifest.record_download(
            release="0325", key=source.key, url=source.url, zip_name=source.zip_name,
            sha256="00" * 32, size_bytes=1,
        )
        if source.key != "activity_summary":
            staged = staging / source.dat_name
            manifest.record_staging(
                release="0325", key=source.key, dat_name=source.dat_name,
                dat_size_bytes=staged.stat().st_size,
                row_count=sum(1 for _ in staged.open()) - 1,
            )


@pytest.fixture
def fixture_settings(tmp_path, migrated_db) -> Settings:
    data_dir = tmp_path / "data"
    _build_fixture_dataset(data_dir)
    return Settings(
        database_url=migrated_db, data_dir=data_dir, release="0325", log_level="INFO"
    )


def test_full_fixture_load(fixture_settings):
    with psycopg.connect(fixture_settings.database_url) as conn:
        results = load_all(conn, fixture_settings)

        counts = {r.table: r.loaded_rows for r in results}
        assert counts == {
            "respondents": 3,
            "household_members": 4,
            "activities": 5,
            "activity_companions": 5,
            "cps_persons": 4,          # nonrespondent household filtered out
            "replicate_weights": 3,
            "pandemic_replicate_weights": 2,
        }

        # typed values survived the round trip
        earnings, hours_vary = conn.execute(
            "SELECT weekly_earnings, usual_hours_vary FROM atus.respondents "
            "WHERE tucaseid = %s", (int(CASE_A),),
        ).fetchone()
        assert earnings == Decimal("660.00")
        assert hours_vary is True

        # 2020 weight semantics
        final, pandemic = conn.execute(
            "SELECT final_weight, pandemic_weight FROM atus.respondents "
            "WHERE tucaseid = %s", (int(CASE_B),),
        ).fetchone()
        assert final is None
        assert pandemic == Decimal("5541150.024906")

        # replicate weights: NULL for the 2020 case, populated otherwise
        assert conn.execute(
            "SELECT tufnwgtp001 FROM atus.replicate_weights WHERE tucaseid = %s",
            (int(CASE_B),),
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT tufnwgtp160 FROM atus.replicate_weights WHERE tucaseid = %s",
            (int(CASE_A),),
        ).fetchone()[0] == Decimal("1000160.000001")

        # the activity hierarchy joins through the lexicon
        name = conn.execute(
            """
            SELECT t1.name FROM atus.activities a
            JOIN atus.activity_tier1 t1 ON t1.code = a.tier1_code
            WHERE a.tucaseid = %s AND a.activity_number = 2
            """,
            (int(CASE_A),),
        ).fetchone()[0]
        assert name == "Work & Work-Related Activities"

        # provenance was recorded for this run (the shared test database may
        # hold source_files rows from other tests' ingestion runs)
        run_id, status = conn.execute(
            "SELECT id, status FROM atus.ingestion_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert status == "succeeded"
        assert conn.execute(
            "SELECT count(*) FROM atus.source_files WHERE ingestion_run_id = %s",
            (run_id,),
        ).fetchone()[0] == 7


def test_failed_load_leaves_database_unchanged(fixture_settings):
    with psycopg.connect(fixture_settings.database_url) as conn:
        load_all(conn, fixture_settings)  # good load first

        # corrupt one staged file: an activity code with valid tier prefixes
        # that does not exist in the lexicon
        bad_path = fixture_settings.staging_dir / "atusact_0325.dat"
        content = bad_path.read_text().replace("120303", "120397")
        bad_path.write_text(content)

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            load_all(conn, fixture_settings)

        # previous complete state is intact, and the failed run is recorded
        assert conn.execute("SELECT count(*) FROM atus.activities").fetchone()[0] == 5
        statuses = [
            row[0] for row in conn.execute(
                "SELECT status FROM atus.ingestion_runs ORDER BY id"
            )
        ]
        assert statuses[-1] == "failed"
        assert "succeeded" in statuses
