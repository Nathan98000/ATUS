"""Hand-computable fixture dataset for analytics integration tests.

Six respondents whose weighted estimates, replicate variances, and filter
behaviors can be verified by pencil-and-paper (expected values are derived in
the test module):

===  ====  ======  ===========================  =====  ==========================
id   year  weight  sleep (010101) minutes       sex    notes
===  ====  ======  ===========================  =====  ==========================
E1   2023  1.0     100 (one episode)            male   education bachelor's (43)
E2   2023  1.0     200 (TEN 20-min episodes)    female CPS education missing (-1)
E3   2023  2.0     0 (plus a 0-minute episode)  female age 70, region 3
P8   2019  1.5     360 (diary in the gap win.)  male   pandemic weight 0.0
P9   2019  1.5     120                          male   pandemic weight 3.0
P0   2020  NULL    240                          female pandemic weight 1.0
===  ====  ======  ===========================  =====  ==========================

Replicate weights (2023 cases, multi-year scheme): E1 = [2, 1, 1, ...];
E2 = [0, 2, 1, ...]; E3 = all 2; P8/P9 all 1.5. Pandemic replicates: P8 all
0.0 (gap window), P9 all 3.0, P0 all 1.0; P0's multi-year replicates are all
-1 (NULL), mirroring the real 2020 data.

The dataset flows through the real Phase 1 loader so the analytics tests also
exercise the actual schema, constraints, and lexicon.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from atus_pipeline.config import Settings
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

REPO_ROOT = Path(__file__).resolve().parents[2]

E1, E2, E3 = "20230101000001", "20230101000002", "20230101000003"
P9, P0 = "20190101000009", "20200101000010"
# P8: a 2019 diary inside the pandemic-excluded window (Mar 18 - May 9), so its
# TU20FWGT is 0 (mirroring the real data): included under multiyear weights,
# excluded entirely under the pandemic scheme.
P8 = "20190101000008"

SLEEP, TV = "010101", "120303"


def _write(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def _episode(case: str, n: int, code: str, start: str, stop: str, dur: int, cum: int,
             dur_uncapped: int | None = None) -> dict[str, str]:
    tier1, tier2 = code[:2], code[:4]
    return fixtures.activity_row(
        TUCASEID=case, TUACTIVITY_N=str(n), TRCODEP=code, TRTIER1P=tier1,
        TRTIER2P=tier2, TUSTARTTIM=start, TUSTOPTIME=stop,
        TUACTDUR24=str(dur), TUACTDUR=str(dur_uncapped if dur_uncapped is not None else dur),
        TUCUMDUR24=str(cum),
    )


def _minutes(hhmm: str) -> str:
    return hhmm + ":00"


def _replicates(prefix: str, values: dict[int, str], default: str) -> dict[str, str]:
    return {
        f"{prefix}{i:03d}": values.get(i, default) for i in range(1, 161)
    }


def build_analytics_dataset(data_dir: Path) -> None:
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
            TUCASEID=E1, TUYEAR="2023", TUDIARYDATE="20230115",
            TUFNWGTP="1.000000", TELFS="1",
        ),
        fixtures.respondent_row(
            TUCASEID=E2, TUYEAR="2023", TUDIARYDATE="20230116", TUDIARYDAY="2",
            TUFNWGTP="1.000000", TELFS="5", TRCHILDNUM="2",
        ),
        fixtures.respondent_row(
            TUCASEID=E3, TUYEAR="2023", TUDIARYDATE="20230117", TUDIARYDAY="3",
            TUFNWGTP="2.000000", TELFS="5",
        ),
        fixtures.respondent_row(
            TUCASEID=P8, TUYEAR="2019", TUDIARYDATE="20190401", TUDIARYDAY="2",
            TUFNWGTP="1.500000", TU20FWGT="0.000000", TELFS="1",
        ),
        fixtures.respondent_row(
            TUCASEID=P9, TUYEAR="2019", TUDIARYDATE="20190602",
            TUFNWGTP="1.500000", TU20FWGT="3.000000", TELFS="1",
        ),
        fixtures.respondent_row(
            TUCASEID=P0, TUYEAR="2020", TUDIARYDATE="20200607",
            TUFNWGTP="-1.000000", TU20FWGT="1.000000", TELFS="5",
        ),
    ])

    _write(staging / "atusrost_0325.dat", ROSTER_COLUMNS, [
        fixtures.roster_row(TUCASEID=E1, TULINENO="1", TERRP="18", TEAGE="30", TESEX="1"),
        fixtures.roster_row(TUCASEID=E2, TULINENO="1", TERRP="18", TEAGE="40", TESEX="2"),
        fixtures.roster_row(TUCASEID=E3, TULINENO="1", TERRP="18", TEAGE="70", TESEX="2"),
        fixtures.roster_row(TUCASEID=P8, TULINENO="1", TERRP="18", TEAGE="35", TESEX="1"),
        fixtures.roster_row(TUCASEID=P9, TULINENO="1", TERRP="18", TEAGE="25", TESEX="1"),
        fixtures.roster_row(TUCASEID=P0, TULINENO="1", TERRP="18", TEAGE="50", TESEX="2"),
    ])

    episodes: list[dict[str, str]] = []
    # E1: one 100-minute sleep episode, then TV to 04:00.
    episodes.append(_episode(E1, 1, SLEEP, "04:00:00", "05:40:00", 100, 100))
    episodes.append(_episode(E1, 2, TV, "05:40:00", "04:00:00", 1340, 1440))
    # E2: TEN 20-minute sleep episodes (grain-safety case), then TV.
    for i in range(10):
        start_min, stop_min = 240 + 20 * i, 240 + 20 * (i + 1)  # minutes after midnight
        start = f"{start_min // 60:02d}:{start_min % 60:02d}:00"
        stop = f"{stop_min // 60:02d}:{stop_min % 60:02d}:00"
        episodes.append(_episode(E2, i + 1, SLEEP, start, stop, 20, 20 * (i + 1)))
    episodes.append(_episode(E2, 11, TV, "07:20:00", "04:00:00", 1240, 1440))
    # E3: TV, a ZERO-minute sleep episode (0 minutes is not participation), TV.
    episodes.append(_episode(E3, 1, TV, "04:00:00", "09:00:00", 300, 300))
    episodes.append(_episode(E3, 2, SLEEP, "09:00:00", "09:00:00", 0, 300))
    episodes.append(_episode(E3, 3, TV, "09:00:00", "04:00:00", 1140, 1440))
    # P8 (2019, gap window): sleep then TV.
    episodes.append(_episode(P8, 1, SLEEP, "04:00:00", "10:00:00", 360, 360))
    episodes.append(_episode(P8, 2, TV, "10:00:00", "04:00:00", 1080, 1440))
    # P9 (2019) and P0 (2020): sleep then TV.
    episodes.append(_episode(P9, 1, SLEEP, "04:00:00", "06:00:00", 120, 120))
    episodes.append(_episode(P9, 2, TV, "06:00:00", "04:00:00", 1320, 1440))
    episodes.append(_episode(P0, 1, SLEEP, "04:00:00", "08:00:00", 240, 240))
    episodes.append(_episode(P0, 2, TV, "08:00:00", "04:00:00", 1200, 1440))
    _write(staging / "atusact_0325.dat", ACTIVITY_COLUMNS, episodes)

    # One "who not asked" placeholder per episode keeps the Who file consistent.
    who_rows = [
        fixtures.who_row(TUCASEID=row["TUCASEID"], TUACTIVITY_N=row["TUACTIVITY_N"])
        for row in episodes
    ]
    _write(staging / "atuswho_0325.dat", WHO_COLUMNS, who_rows)

    _write(staging / "atuscps_0325.dat", CPS_REQUIRED_COLUMNS, [
        fixtures.cps_row(TUCASEID=E1, TULINENO="1", PEEDUCA="43", GEREG="1"),
        fixtures.cps_row(TUCASEID=E2, TULINENO="1", PEEDUCA="-1", GEREG="2"),
        fixtures.cps_row(TUCASEID=E3, TULINENO="1", PEEDUCA="39", GEREG="3"),
        fixtures.cps_row(TUCASEID=P8, TULINENO="1", PEEDUCA="40", GEREG="4"),
        fixtures.cps_row(TUCASEID=P9, TULINENO="1", PEEDUCA="40", GEREG="4"),
        fixtures.cps_row(TUCASEID=P0, TULINENO="1", PEEDUCA="40", GEREG="4"),
    ])

    _write(staging / "atuswgts_0325.dat", REPLICATE_WEIGHT_COLUMNS, [
        fixtures.replicate_weights_row(
            TUCASEID=E1, **_replicates("TUFNWGTP", {1: "2.000000"}, "1.000000"),
        ),
        fixtures.replicate_weights_row(
            TUCASEID=E2, **_replicates("TUFNWGTP", {1: "0.000000", 2: "2.000000"}, "1.000000"),
        ),
        fixtures.replicate_weights_row(
            TUCASEID=E3, **_replicates("TUFNWGTP", {}, "2.000000"),
        ),
        fixtures.replicate_weights_row(
            TUCASEID=P8, **_replicates("TUFNWGTP", {}, "1.500000"),
        ),
        fixtures.replicate_weights_row(
            TUCASEID=P9, **_replicates("TUFNWGTP", {}, "1.500000"),
        ),
        fixtures.replicate_weights_row(
            TUCASEID=P0, **_replicates("TUFNWGTP", {}, "-1.000000"),
        ),
    ])

    _write(staging / "atuswgtspan_1920.dat", PANDEMIC_WEIGHT_COLUMNS, [
        fixtures.pandemic_weights_row(
            TUCASEID=P8, **_replicates("TU20FWGT", {}, "0.000000"),
        ),
        fixtures.pandemic_weights_row(
            TUCASEID=P9, **_replicates("TU20FWGT", {}, "3.000000"),
        ),
        fixtures.pandemic_weights_row(
            TUCASEID=P0, **_replicates("TU20FWGT", {}, "1.000000"),
        ),
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


def analytics_settings(tmp_path: Path, database_url: str) -> Settings:
    data_dir = tmp_path / "data"
    build_analytics_dataset(data_dir)
    return Settings(
        database_url=database_url, data_dir=data_dir, release="0325", log_level="INFO"
    )
