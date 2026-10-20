"""A second version of the analytics fixture, for reload tests.

``make_second_version`` edits a fixture built by
``analytics_dataset.analytics_settings`` in place so that a reload produces
numbers that differ from the first version in every table the engine joins,
and so that any mix of old and new tables produces a third, recognizable
value:

=============================  =========  =========  ==========================
sleep, 2023, mean min/day      version 1  version 2  mixed
=============================  =========  =========  ==========================
E1 sleep minutes (activities)  100        160
E1 weight (respondents)        1.0        2.0
estimate                       75.0       104.0      90.0 (new activities, old
                                                     weights), 80.0 (the reverse)
respondents (``/meta``)        6          5          (P8 removed from every file)
=============================  =========  =========  ==========================

Version 2: (2·160 + 1·200 + 2·0) / (2 + 1 + 2) = 520 / 5 = 104.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

from atus_pipeline.config import Settings
from atus_pipeline.sources import get_source, release_files
from tests.integration.analytics_dataset import E1, P8, record_manifest

V1_SLEEP_2023 = 75.0
V2_SLEEP_2023 = 104.0
MIXED_SLEEP_2023 = (90.0, 80.0)
V1_RESPONDENTS = 6
V2_RESPONDENTS = 5


def rewrite_staged(
    settings: Settings, key: str, edit: Callable[[dict[str, str]], dict[str, str] | None]
) -> None:
    """Apply ``edit`` to every row of one staged file (``None`` drops the row)."""
    path: Path = settings.staging_dir / get_source(settings.release, key).dat_name
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames)
        rows = [edited for row in reader if (edited := edit(row)) is not None]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def make_second_version(settings: Settings) -> None:
    def respondent(row):
        if row["TUCASEID"] == E1:
            row["TUFNWGTP"] = "2.000000"
        return row

    def activity(row):
        if row["TUCASEID"] == E1 and row["TUACTIVITY_N"] == "1":
            row.update(TUSTOPTIME="06:40:00", TUACTDUR24="160", TUACTDUR="160",
                       TUCUMDUR24="160")
        elif row["TUCASEID"] == E1 and row["TUACTIVITY_N"] == "2":
            row.update(TUSTARTTIM="06:40:00", TUACTDUR24="1280", TUACTDUR="1280")
        return row

    rewrite_staged(settings, "respondent", respondent)
    rewrite_staged(settings, "activity", activity)
    for source in release_files(settings.release):
        if source.load:
            rewrite_staged(
                settings, source.key, lambda row: None if row["TUCASEID"] == P8 else row
            )
    record_manifest(settings.data_dir, settings.release)
