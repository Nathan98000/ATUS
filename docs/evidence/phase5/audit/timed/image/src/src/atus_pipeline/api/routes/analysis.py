"""Analysis endpoints: thin translations between HTTP and the Phase 2 engine.

Each handler: validated request -> AnalysisSpec -> cache lookup ->
AnalysisEngine -> result.to_dict() -> typed response. No statistical logic
lives here. Responses carry two observability headers:

* ``X-Analysis-Key`` — the deterministic identifier of this analysis
  (hash of operation + canonical spec + analytics version + data version). It is a
  reproducibility/cache key, not a stored record ID.
* ``X-Cache`` — ``hit`` or ``miss`` for the server-side result cache.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import psycopg
from fastapi import APIRouter, Depends, Response

from ...analytics import ANALYTICS_VERSION, AnalysisEngine
from ...analytics.spec import population_to_dict
from ..cache import AnalysisCache, cache_key
from ..dependencies import (
    get_cache,
    get_connection,
    get_data_version,
    get_engine,
    read_data_version,
)
from ..schemas.analysis import (
    CompareRequest,
    CompareResponse,
    EstimateRequest,
    EstimateResponse,
    TrendRequest,
    TrendResponse,
)
from ..schemas.errors import ErrorResponse

log = logging.getLogger("atus.api")

router = APIRouter(prefix="/analysis", tags=["analysis"])

_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Malformed JSON body"},
    422: {
        "model": ErrorResponse,
        "description": "Schema violation, or analytically invalid/unsupported request "
                       "(e.g. 2020 under the multiyear weight scheme)",
    },
    503: {"model": ErrorResponse, "description": "Analytical database unavailable"},
}


def _cached_analysis(
    *,
    operation: str,
    spec_payload: dict,
    cache: AnalysisCache,
    data_version: str,
    conn: psycopg.Connection,
    response: Response,
    compute: Callable[[], dict],
) -> dict:
    """Serve from the result cache or compute-and-store. Only successful
    results are cached (a raising `compute` stores nothing), and a cache
    failure degrades to computing — never to an error.

    Concurrent-reload guard: `atus load` rebuilds the data mid-flight of a
    request in rare cases, so the data version is re-read after computing and
    the result is only cached if the version is unchanged — a result computed
    against reloaded (or mixed-snapshot) data can never be stored under the
    pre-reload key. Such a result is still returned once, uncached and marked
    `X-Cache: bypass`.
    """
    key = cache_key(operation, spec_payload, ANALYTICS_VERSION, data_version)
    response.headers["X-Analysis-Key"] = key
    try:
        hit = cache.get(key)
    except Exception:  # cache trouble must never break analysis
        log.exception("analysis cache get failed; computing without cache")
        hit = None
    if hit is not None:
        response.headers["X-Cache"] = "hit"
        return hit
    payload = compute()
    if read_data_version(conn) != data_version:
        log.warning(
            "data version changed during %s computation; result returned uncached",
            operation,
        )
        response.headers["X-Cache"] = "bypass"
        return payload
    try:
        cache.put(key, payload)
    except Exception:
        log.exception("analysis cache put failed; returning uncached result")
    response.headers["X-Cache"] = "miss"
    return payload


@router.post(
    "/estimate",
    response_model=EstimateResponse,
    responses=_ERROR_RESPONSES,
    summary="Single (or pooled multi-year) estimate",
    description=(
        "Computes one survey-weighted estimate for the selected activity, "
        "population, and years, with an official replicate-weight standard "
        "error and confidence interval unless `variance` is `none`. Multi-year "
        "requests are pooled into one estimate (use the trend endpoint for "
        "per-year series). Estimates are weighted population statistics per "
        "ATUS User's Guide ch. 7 — never unweighted sample means."
    ),
)
def estimate(
    body: EstimateRequest,
    response: Response,
    engine: AnalysisEngine = Depends(get_engine),
    cache: AnalysisCache = Depends(get_cache),
    data_version: str = Depends(get_data_version),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict:
    spec = body.to_spec()
    return _cached_analysis(
        operation="estimate",
        spec_payload=spec.to_dict(),
        cache=cache,
        data_version=data_version,
        conn=conn,
        response=response,
        compute=lambda: engine.estimate(spec).to_dict(),
    )


@router.post(
    "/trend",
    response_model=TrendResponse,
    responses=_ERROR_RESPONSES,
    summary="Per-year trend",
    description=(
        "Computes one estimate per requested year. Years that cannot be "
        "validly estimated under the requested weight scheme (2020 under "
        "`multiyear`) are returned as explicit unavailable points with a "
        "reason — never silently dropped, and never serialized as zero."
    ),
)
def trend(
    body: TrendRequest,
    response: Response,
    engine: AnalysisEngine = Depends(get_engine),
    cache: AnalysisCache = Depends(get_cache),
    data_version: str = Depends(get_data_version),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict:
    spec = body.to_spec()
    return _cached_analysis(
        operation="trend",
        spec_payload=spec.to_dict(),
        cache=cache,
        data_version=data_version,
        conn=conn,
        response=response,
        compute=lambda: engine.trend(spec).to_dict(),
    )


@router.post(
    "/compare",
    response_model=CompareResponse,
    responses=_ERROR_RESPONSES,
    summary="Two-population comparison",
    description=(
        "Estimates the same measure for two populations (each group filter is "
        "merged onto the shared `population` filter) plus their difference "
        "(group_a − group_b). The difference's standard error is computed from "
        "per-replicate differences, which accounts for the covariance between "
        "the overlapping-sample group estimates."
    ),
)
def compare(
    body: CompareRequest,
    response: Response,
    engine: AnalysisEngine = Depends(get_engine),
    cache: AnalysisCache = Depends(get_cache),
    data_version: str = Depends(get_data_version),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict:
    comparison = body.to_comparison()
    spec_payload = {
        "base": comparison.base.to_dict(),
        "group_a": population_to_dict(comparison.group_a),
        "group_b": population_to_dict(comparison.group_b),
        "label_a": comparison.label_a,
        "label_b": comparison.label_b,
    }
    return _cached_analysis(
        operation="compare",
        spec_payload=spec_payload,
        cache=cache,
        data_version=data_version,
        conn=conn,
        response=response,
        compute=lambda: engine.compare(comparison).to_dict(),
    )
