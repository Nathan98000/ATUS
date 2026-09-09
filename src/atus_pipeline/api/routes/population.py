"""Population-metadata endpoint: the filter vocabulary a client can use."""

from __future__ import annotations

from fastapi import APIRouter, Response

from ...analytics.population import describe_dimensions
from ..schemas.population import PopulationDimension, PopulationMetadataResponse

router = APIRouter(prefix="/population", tags=["population"])


@router.get(
    "/metadata",
    response_model=PopulationMetadataResponse,
    summary="Supported population filter dimensions",
    description=(
        "Every population dimension analysis requests may filter on, with "
        "valid values for categorical dimensions. Served from the analytical "
        "engine's own dimension registry, so this vocabulary cannot drift "
        "from what filtering actually accepts."
    ),
)
def population_metadata(response: Response) -> PopulationMetadataResponse:
    response.headers["Cache-Control"] = "public, max-age=300"
    return PopulationMetadataResponse(
        dimensions=[PopulationDimension(**d) for d in describe_dimensions()],
        missing_data_rule=(
            "Filtering on a dimension excludes respondents whose value is missing "
            "for that dimension (from numerator and denominator alike); missing is "
            "never treated as 'no'. Unfiltered dimensions include everyone."
        ),
        default_universe=(
            "U.S. civilian noninstitutional population age 15 and over "
            "(the ATUS target population)."
        ),
    )
