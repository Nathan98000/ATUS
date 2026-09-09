"""Activity-lexicon response contracts."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ActivityEntry(BaseModel):
    code: str = Field(description="Harmonized 2003-25 lexicon code (2, 4, or 6 digits).")
    name: str
    level: int = Field(description="1 = major category, 2 = subcategory, 3 = 6-digit activity.")
    parent_code: str | None = Field(
        default=None, description="Immediate parent code (null for major categories)."
    )
    harmonization_note: str | None = Field(
        default=None,
        description="BLS note on retired codes this harmonized code absorbs (level 3 only).",
    )


class ActivityListResponse(BaseModel):
    activities: list[ActivityEntry]
    total: int = Field(description="Entries returned after filters (the lexicon is small).")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "activities": [
                        {"code": "01", "name": "Personal Care Activities", "level": 1,
                         "parent_code": None, "harmonization_note": None},
                        {"code": "0101", "name": "Sleeping", "level": 2,
                         "parent_code": "01", "harmonization_note": None},
                        {"code": "010101", "name": "Sleeping", "level": 3,
                         "parent_code": "0101", "harmonization_note": None},
                    ],
                    "total": 3,
                }
            ]
        }
    }


class ActivityDetailResponse(BaseModel):
    code: str
    name: str
    level: int
    parents: list[ActivityEntry] = Field(description="Ancestors, outermost first.")
    children: list[ActivityEntry] = Field(description="Direct children (empty for level 3).")
    harmonization_note: str | None = None
    descendant_leaf_count: int = Field(
        description="Number of 6-digit codes selected when this code is used in an "
                    "analysis (a tier code means itself plus all descendants)."
    )


class ActivityPreset(BaseModel):
    name: str = Field(description="Value to use as {'preset': name} in analysis requests.")
    label: str
    include: list[str]
    exclude: list[str]


class ActivityPresetsResponse(BaseModel):
    presets: list[ActivityPreset]
