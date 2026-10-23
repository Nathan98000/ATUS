"""System endpoint contracts (liveness)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]
    build: str = Field(
        description="Build identifier of the running image (ATUS_BUILD_SHA; 'dev' locally). "
                    "The deploy workflow waits until it equals the deployed commit."
    )
    expected_data_version: str | None = Field(
        default=None,
        description="The data version this deployment is bound to (ATUS_EXPECTED_DATA_VERSION;"
                    " null when unset), read from the configuration, never the database. The "
                    "deploy workflow publishes a release's snapshot only once it equals that "
                    "release's data_version.",
    )
