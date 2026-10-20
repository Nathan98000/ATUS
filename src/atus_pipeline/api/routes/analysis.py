"""Analysis endpoints: thin translations between HTTP and the Phase 2 engine.

Each handler: validated request -> AnalysisSpec -> AnalysisRunner (cache
lookup, single-flight, admission) -> AnalysisEngine -> result.to_dict() ->
typed response. No statistical logic lives here, and nothing touches the
database before the request body has been validated. Responses carry
observability headers:

* ``X-Analysis-Key`` — the deterministic identifier of this analysis
  (hash of operation + canonical spec + analytics version + data version). It is a
  reproducibility/cache key, not a stored record ID.
* ``X-Cache`` — ``hit``, ``miss``, ``join`` (shared an identical computation
  already in progress) or ``bypass`` (computed during a data reload; not
  cached).
* ``X-Data-Version``, ``X-Analytics-Version``, ``X-Response-Format`` — what
  the response bytes depend on (edge caches key on them).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ...analytics.spec import population_to_dict
from ..analysis_runner import AnalysisJob
from ..dependencies import AnalysisRuntime, get_analysis_runtime
from ..schemas.analysis import (
    CompareRequest,
    CompareResponse,
    EstimateRequest,
    EstimateResponse,
    TrendRequest,
    TrendResponse,
)
from ..schemas.errors import ErrorResponse

router = APIRouter(prefix="/analysis", tags=["analysis"])

_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Malformed JSON body"},
    413: {"model": ErrorResponse, "description": "Request body too large (payload_too_large)"},
    415: {"model": ErrorResponse, "description": "Body is not JSON (unsupported_media_type)"},
    422: {
        "model": ErrorResponse,
        "description": "Schema violation, or analytically invalid/unsupported request "
                       "(e.g. 2020 under the multiyear weight scheme)",
    },
    429: {
        "model": ErrorResponse,
        "description": "Per-client rate limit (rate_limited); see Retry-After",
    },
    503: {
        "model": ErrorResponse,
        "description": "Temporarily unable to compute: overloaded, analysis_timeout, "
                       "daily_capacity_reached, database_unavailable, database_paused, "
                       "data_not_loaded; see Retry-After",
    },
}


async def _run(
    job: AnalysisJob, request: Request, response: Response, runtime: AnalysisRuntime
) -> dict:
    return await runtime.runner.run(
        job, request=request, response=response, cache=runtime.cache,
        db=runtime.db, engine_factory=runtime.engine_factory,
    )


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
async def estimate(
    body: EstimateRequest,
    request: Request,
    response: Response,
    runtime: AnalysisRuntime = Depends(get_analysis_runtime),
) -> dict:
    spec = body.to_spec()
    job = AnalysisJob(
        operation="estimate",
        spec_payload=spec.to_dict(),
        years=spec.years,
        compute=lambda engine: engine.estimate(spec).to_dict(),
    )
    return await _run(job, request, response, runtime)


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
async def trend(
    body: TrendRequest,
    request: Request,
    response: Response,
    runtime: AnalysisRuntime = Depends(get_analysis_runtime),
) -> dict:
    spec = body.to_spec()
    job = AnalysisJob(
        operation="trend",
        spec_payload=spec.to_dict(),
        years=spec.years,
        compute=lambda engine: engine.trend(spec).to_dict(),
    )
    return await _run(job, request, response, runtime)


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
async def compare(
    body: CompareRequest,
    request: Request,
    response: Response,
    runtime: AnalysisRuntime = Depends(get_analysis_runtime),
) -> dict:
    comparison = body.to_comparison()
    spec_payload = {
        "base": comparison.base.to_dict(),
        "group_a": population_to_dict(comparison.group_a),
        "group_b": population_to_dict(comparison.group_b),
        "label_a": comparison.label_a,
        "label_b": comparison.label_b,
    }
    job = AnalysisJob(
        operation="compare",
        spec_payload=spec_payload,
        years=comparison.base.years,
        compute=lambda engine: engine.compare(comparison).to_dict(),
    )
    return await _run(job, request, response, runtime)
