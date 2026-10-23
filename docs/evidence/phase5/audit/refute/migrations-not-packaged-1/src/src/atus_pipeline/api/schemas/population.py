"""Population-metadata response contract."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PopulationDimension(BaseModel):
    name: str = Field(description="Field name to use inside analysis 'population' objects.")
    type: str = Field(description="integer | category | boolean | date")
    description: str
    values: list | None = Field(
        default=None, description="Valid values for categorical dimensions."
    )
    unit: str | None = None
    notes: str | None = None


class PopulationMetadataResponse(BaseModel):
    dimensions: list[PopulationDimension]
    missing_data_rule: str = Field(
        description="How missing values interact with filters (applies to every dimension)."
    )
    default_universe: str = Field(
        description="The population when no filters are set."
    )
