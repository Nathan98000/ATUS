"""Data-quality tests for the committed activity lexicon reference CSV.

The CSV is generated from the official BLS lexicon PDF by
scripts/extract_lexicon.py and committed; these tests guard its structural
integrity so a bad regeneration cannot slip in unnoticed.
"""

import csv
import re
from pathlib import Path

import pytest

LEXICON = Path(__file__).resolve().parents[2] / "data" / "reference" / "activity_lexicon_0325.csv"


@pytest.fixture(scope="module")
def rows() -> list[dict[str, str]]:
    with LEXICON.open(newline="") as fh:
        return list(csv.DictReader(fh))


def by_level(rows, level):
    return [r for r in rows if r["level"] == level]


def test_expected_counts(rows):
    # The 2003-25 lexicon defines 18 major categories and 431 6-digit codes
    # (matching the t-columns of the BLS activity summary file).
    assert len(by_level(rows, "1")) == 18
    assert len(by_level(rows, "2")) == 107
    assert len(by_level(rows, "3")) == 431


def test_code_formats(rows):
    for row in rows:
        width = {"1": 2, "2": 4, "3": 6}[row["level"]]
        assert re.fullmatch(rf"\d{{{width}}}", row["code"]), row


def test_no_duplicate_codes(rows):
    codes = [(r["level"], r["code"]) for r in rows]
    assert len(codes) == len(set(codes))


def test_hierarchy_is_closed(rows):
    tier1 = {r["code"] for r in by_level(rows, "1")}
    tier2 = {r["code"] for r in by_level(rows, "2")}
    for row in by_level(rows, "2"):
        assert row["code"][:2] in tier1, row
    for row in by_level(rows, "3"):
        assert row["code"][:4] in tier2, row


def test_names_are_nonempty_and_clean(rows):
    for row in rows:
        assert row["name"].strip(), row
        assert "  " not in row["name"], row


def test_known_entries(rows):
    """Spot-checks against the published lexicon."""
    named = {(r["level"], r["code"]): r["name"] for r in rows}
    assert named[("1", "01")] == "Personal Care Activities"
    assert named[("1", "18")] == "Traveling"
    assert named[("1", "50")] == "Data Codes"
    assert named[("2", "0101")] == "Sleeping"
    assert named[("3", "010101")] == "Sleeping"
    assert named[("3", "050101")].startswith("Work, main job")


def test_travel_uses_harmonized_tier_18_not_17(rows):
    """BLS moved travel from tier 17 to 18 in 2005; the multi-year lexicon
    harmonizes everything to 18."""
    tier1 = {r["code"] for r in by_level(rows, "1")}
    assert "18" in tier1
    assert "17" not in tier1


def test_harmonization_notes_reference_absorbed_codes(rows):
    """Harmonized codes (e.g. 020681) document which retired codes they absorb."""
    noted = {r["code"]: r["harmonization_note"] for r in by_level(rows, "3")}
    assert "020601" in noted["020681"]
    assert noted["030186"], "030186 absorbs 030106/030107 and must carry a note"
