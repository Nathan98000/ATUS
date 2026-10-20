"""API metadata + capability discovery.

The data facts (release, years, respondent count, ingestion run) come from
the in-process data snapshot, so a fresh snapshot answers ``/meta`` with no
database statement (audit F14, F62).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ...analytics import ANALYTICS_VERSION
from ...analytics.estimators import MEASURE_UNITS
from ...analytics.population import describe_dimensions
from ...analytics.spec import ACTIVITY_PRESETS, Measure
from .. import API_VERSION
from ..database import Database
from ..dependencies import get_database, get_snapshots
from ..schemas.meta import (
    CapabilitiesInfo,
    DataInfo,
    MeasureInfo,
    MetaResponse,
    WeightSchemeInfo,
)
from ..snapshot import SnapshotStore

router = APIRouter(prefix="/meta", tags=["meta"])

_MEASURE_DESCRIPTIONS = {
    Measure.AVERAGE_MINUTES_PER_DAY:
        "Σ(w·minutes)/Σ(w): average minutes per day across the whole selected "
        "population, participants and non-participants alike.",
    Measure.PARTICIPATION_RATE:
        "Σ(w·participant)/Σ(w): share of the population doing the activity on an "
        "average day (a proportion 0-1; a daily rate, not a longer-period one).",
    Measure.AVERAGE_MINUTES_PER_PARTICIPANT:
        "Σ(w·minutes)/Σ(w·participant): average minutes per day among those who "
        "did the activity that day.",
    Measure.PARTICIPANTS_PER_DAY:
        "Σ(w·participant)/days: number of persons doing the activity on an "
        "average day of the period.",
}


@router.get(
    "",
    response_model=MetaResponse,
    summary="API, analytics, and data versions plus capability discovery",
    description=(
        "What this service can compute and against which data. `api_version` is "
        "the HTTP contract; `analytics_version` is the statistical "
        "implementation (a change means computed numbers may differ); `data` "
        "identifies the loaded BLS release and ingestion run. Clients should "
        "discover capabilities here instead of hard-coding them."
    ),
)
async def meta(
    response: Response,
    db: Database = Depends(get_database),
    snapshots: SnapshotStore = Depends(get_snapshots),
) -> MetaResponse:
    snapshot = await snapshots.get(db)
    run = snapshot.run

    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["X-Data-Version"] = snapshot.data_version
    response.headers["X-Analytics-Version"] = ANALYTICS_VERSION
    return MetaResponse(
        api_version=API_VERSION,
        analytics_version=ANALYTICS_VERSION,
        data=DataInfo(
            release=run.release if run else "unloaded",
            years=list(snapshot.years),
            respondents=snapshot.respondents,
            last_ingested_at=run.finished_at.isoformat() if run and run.finished_at else None,
            ingestion_run_id=run.id if run else None,
        ),
        capabilities=CapabilitiesInfo(
            measures=[
                MeasureInfo(
                    name=measure.value,
                    unit=MEASURE_UNITS[measure],
                    description=_MEASURE_DESCRIPTIONS[measure],
                )
                for measure in Measure
            ],
            weight_schemes=[
                WeightSchemeInfo(
                    name="multiyear", bls_variable="TUFNWGTP",
                    valid_years="2003-2019, 2021 and later (refuses 2020)",
                    description="BLS multi-year weight, comparable across years.",
                ),
                WeightSchemeInfo(
                    name="pandemic", bls_variable="TU20FWGT",
                    valid_years="2019 and 2020 only",
                    description="2020-method weight representing the collection-"
                                "comparable windows Jan 1-Mar 17 and May 10-Dec 31.",
                ),
            ],
            variance_methods=["replicate", "none"],
            confidence_level_default=0.95,
            activity_presets=sorted(ACTIVITY_PRESETS),
            population_dimensions=[d["name"] for d in describe_dimensions()],
            limits={
                "max_years_per_request": 50,
                "max_include_codes_per_selection": 100,
                "max_exclude_codes_per_selection": 100,
            },
            unsupported=[
                "medians and percentiles (distributional statistics)",
                "episode-context analyses (location, time of day, who was present)",
                "ATUS module data (Eating & Health, Well-Being, Leave) and module weights",
                "significance tests (estimates, SEs, and CIs only)",
            ],
        ),
    )
