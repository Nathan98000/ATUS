"""A seeded, randomized fixture dataset for equivalence tests.

The hand-computed fixture (``analytics_dataset.py``) has six respondents:
enough to verify formulas, too few to exercise query rewrites and the
serving database's reshaping over many specifications. This dataset has a
few hundred respondents over several years with the features the serving DB
and the engine's SQL must handle exactly as the full database does:

* many episodes per respondent, repeated codes (pre-aggregation), and
  0-minute episodes (a 0 sum is not participation);
* household rosters and CPS households with lines other than 1;
* missing CPS education and missing household-children counts (NULLs);
* 2020 respondents with no ``TUFNWGTP`` and all-NULL replicate rows (omitted
  by the serving DB), 2019 diaries in the pandemic gap window with zero
  ``TU20FWGT`` and all-zero pandemic replicates (omitted), zero final weights;
* replicate weights with six decimals (lossless as ``float8``).

It flows through the real loader like the hand-computed fixture.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from atus_pipeline.config import DEFAULT_RELEASE, Settings
from atus_pipeline.sources import (
    ACTIVITY_COLUMNS,
    CPS_REQUIRED_COLUMNS,
    PANDEMIC_WEIGHT_COLUMNS,
    REPLICATE_WEIGHT_COLUMNS,
    RESPONDENT_COLUMNS,
    ROSTER_COLUMNS,
    WHO_COLUMNS,
    get_source,
    lexicon_path,
)
from tests import fixtures
from tests.integration.analytics_dataset import REPO_ROOT, record_manifest

YEARS = (2018, 2019, 2020, 2021, 2023)
# Leaf codes from the 0325 lexicon across many tier-1 groups (sleep and TV
# drawn more often, like the real diaries).
CODES = (
    "010101", "010101", "010101", "010201", "020101", "030101", "050101", "050101",
    "060101", "070101", "080101", "110101", "110101", "120101", "120303", "120303",
    "120312", "130101", "150101", "160101", "180101", "181101", "181201", "181301",
    "500101",
)
STATES = ("6", "36", "48", "12", "1")
GAP_START, GAP_END = (3, 18), (5, 9)


@dataclass(frozen=True)
class RandomDataset:
    years: tuple[int, ...]
    respondents: int


def _write(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def _in_gap(day: date) -> bool:
    return GAP_START <= (day.month, day.day) <= GAP_END


def _weight(rng: random.Random) -> str:
    return f"{rng.uniform(1_000, 40_000_000):.6f}"


def _episodes(rng: random.Random, case: str) -> list[dict[str, str]]:
    """A diary day: episodes from 04:00 to 04:00 summing to 1440 minutes."""
    episodes = []
    cumulative = 0
    n = 1
    while cumulative < 1440:
        if n > 1 and rng.random() < 0.05:
            duration = 0                       # a 0-minute episode
        else:
            duration = min(rng.choice((5, 10, 15, 30, 45, 60, 90, 120, 240, 480)),
                           1440 - cumulative)
        code = rng.choice(CODES)
        start = 240 + cumulative
        stop = start + duration
        cumulative += duration
        episodes.append(fixtures.activity_row(
            TUCASEID=case, TUACTIVITY_N=str(n), TRCODEP=code, TRTIER1P=code[:2],
            TRTIER2P=code[:4],
            TUSTARTTIM=f"{start // 60 % 24:02d}:{start % 60:02d}:00",
            TUSTOPTIME=f"{stop // 60 % 24:02d}:{stop % 60:02d}:00",
            TUACTDUR24=str(duration), TUACTDUR=str(duration),
            TUCUMDUR24=str(max(cumulative, 1)),
        ))
        n += 1
    return episodes


def build_random_dataset(data_dir: Path, *, seed: int = 20261020,
                         respondents_per_year: int = 60,
                         release: str = DEFAULT_RELEASE) -> RandomDataset:
    rng = random.Random(seed)
    staging = data_dir / "staging" / release
    staging.mkdir(parents=True)
    reference = data_dir / "reference"
    reference.mkdir(parents=True)
    lexicon = lexicon_path(REPO_ROOT / "data" / "reference", release)
    lexicon_path(reference, release).write_bytes(lexicon.read_bytes())

    respondents, roster, cps, episodes, reps, pandemic = [], [], [], [], [], []
    for year in YEARS:
        for i in range(1, respondents_per_year + 1):
            case = f"{year}{i:010d}"
            day = date(year, 1, 1) + timedelta(days=rng.randrange(365))
            if year == 2020:
                while _in_gap(day):                # no 2020 diaries in the gap window
                    day = date(year, 1, 1) + timedelta(days=rng.randrange(365))
            weight = "-1.000000" if year == 2020 else (
                "0.000000" if rng.random() < 0.03 else _weight(rng))
            pandemic_weight = "-1.000000"
            if year in (2019, 2020):
                pandemic_weight = "0.000000" if _in_gap(day) else _weight(rng)
            respondents.append(fixtures.respondent_row(
                TUCASEID=case, TUYEAR=str(year), TUDIARYDATE=day.strftime("%Y%m%d"),
                TUDIARYDAY=str(day.isoweekday() % 7 + 1), TELFS=str(rng.randint(1, 5)),
                TRCHILDNUM=str(rng.choice((-1, 0, 0, 1, 2, 3))),
                TUFNWGTP=weight, TU20FWGT=pandemic_weight,
            ))
            for line in range(1, 2 + rng.randrange(3)):
                roster.append(fixtures.roster_row(
                    TUCASEID=case, TULINENO=str(line),
                    TERRP="18" if line == 1 else str(rng.randint(20, 40)),
                    TEAGE=str(rng.randint(15, 85) if line == 1 else rng.randint(0, 85)),
                    TESEX=str(rng.randint(1, 2)),
                ))
            for line in range(1, 2 + rng.randrange(2)):
                cps.append(fixtures.cps_row(
                    TUCASEID=case, TULINENO=str(line),
                    PEEDUCA=str(rng.choice((-1, *range(31, 47)))),
                    GEREG=str(rng.randint(1, 4)), GESTFIPS=rng.choice(STATES),
                ))
            episodes.extend(_episodes(rng, case))
            values = {
                f"TUFNWGTP{r:03d}": "-1.000000" if year == 2020 else (
                    "0.000000" if rng.random() < 0.02 else _weight(rng))
                for r in range(1, 161)
            }
            reps.append(fixtures.replicate_weights_row(TUCASEID=case, **values))
            if year in (2019, 2020):
                zero = pandemic_weight == "0.000000"
                pandemic.append(fixtures.pandemic_weights_row(TUCASEID=case, **{
                    f"TU20FWGT{r:03d}": "0.000000" if zero else _weight(rng)
                    for r in range(1, 161)
                }))

    def staged(key: str) -> Path:
        return staging / get_source(release, key).dat_name

    _write(staged("respondent"), RESPONDENT_COLUMNS, respondents)
    _write(staged("roster"), ROSTER_COLUMNS, roster)
    _write(staged("activity"), ACTIVITY_COLUMNS, episodes)
    _write(staged("who"), WHO_COLUMNS, [
        fixtures.who_row(TUCASEID=row["TUCASEID"], TUACTIVITY_N=row["TUACTIVITY_N"])
        for row in episodes
    ])
    _write(staged("cps"), CPS_REQUIRED_COLUMNS, cps)
    _write(staged("replicate_weights"), REPLICATE_WEIGHT_COLUMNS, reps)
    _write(staged("pandemic_weights"), PANDEMIC_WEIGHT_COLUMNS, pandemic)
    record_manifest(data_dir, release)
    return RandomDataset(years=YEARS, respondents=len(respondents))


def random_settings(tmp_path: Path, database_url: str, **kwargs) -> Settings:
    data_dir = tmp_path / "data"
    build_random_dataset(data_dir, **kwargs)
    return Settings(database_url=database_url, data_dir=data_dir, release=DEFAULT_RELEASE,
                    log_level="INFO")
