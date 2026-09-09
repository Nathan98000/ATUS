"""API metadata / capability response contract."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DataInfo(BaseModel):
    release: str = Field(description="BLS multi-year release code, e.g. '0325' = 2003-2025.")
    years: list[int] = Field(description="Survey years loaded and queryable.")
    respondents: int = Field(description="Unweighted respondent count in the database.")
    last_ingested_at: str | None = Field(
        default=None, description="Timestamp of the last successful ingestion run (ISO-8601)."
    )
    ingestion_run_id: int | None = Field(
        default=None, description="Identifier of the last successful ingestion run."
    )


class MeasureInfo(BaseModel):
    name: str
    unit: str
    description: str


class WeightSchemeInfo(BaseModel):
    name: str
    bls_variable: str
    valid_years: str
    description: str


class CapabilitiesInfo(BaseModel):
    measures: list[MeasureInfo]
    weight_schemes: list[WeightSchemeInfo]
    variance_methods: list[str]
    confidence_level_default: float
    activity_presets: list[str]
    population_dimensions: list[str]
    limits: dict = Field(description="Request limits (max years, max activity codes, ...).")
    unsupported: list[str] = Field(
        description="Explicitly out-of-scope analysis types (do not infer support)."
    )


class MetaResponse(BaseModel):
    api_version: str = Field(description="HTTP contract version (path prefix /api/v1).")
    analytics_version: str = Field(
        description="Statistical implementation version — a change means computed "
                    "results may differ."
    )
    data: DataInfo
    capabilities: CapabilitiesInfo
