"""The one error envelope every non-2xx response uses (documented in OpenAPI)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(
        description="Stable machine-readable error code, e.g. 'unsupported_analysis'."
    )
    message: str = Field(description="Human-readable explanation.")
    details: dict | None = Field(
        default=None, description="Optional structured context (never stack traces)."
    )


class ErrorResponse(BaseModel):
    error: ErrorDetail

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "error": {
                        "code": "unsupported_analysis",
                        "message": (
                            "TUFNWGTP (the multi-year weight) is undefined for 2020 "
                            "because ATUS data collection was suspended Mar 18 - May 9, "
                            "2020 and the year was reweighted with a different method "
                            "(TU20FWGT). A single estimate pooling 2020 with this scheme "
                            "is not statistically valid. Estimate 2020 separately with "
                            "weights='pandemic' (years may only be 2019 and/or 2020 "
                            "under that scheme)."
                        ),
                        "details": None,
                    }
                }
            ]
        }
    }
