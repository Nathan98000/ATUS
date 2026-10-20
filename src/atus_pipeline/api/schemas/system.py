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
