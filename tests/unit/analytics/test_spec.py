"""Validation and serialization of analysis specifications."""

from datetime import date

import pytest

from atus_pipeline.analytics.errors import InvalidSpecError
from atus_pipeline.analytics.spec import (
    ACTIVITY_PRESETS,
    ActivitySelector,
    AnalysisSpec,
    ComparisonSpec,
    Measure,
    PopulationFilter,
    merge_filters,
)


def make_spec(**overrides) -> AnalysisSpec:
    defaults = dict(
        measure=Measure.AVERAGE_MINUTES_PER_DAY,
        activity=ActivitySelector(("0101",)),
        years=(2025,),
    )
    defaults.update(overrides)
    return AnalysisSpec(**defaults)


class TestActivitySelector:
    def test_valid_code_widths(self):
        ActivitySelector(("01", "0101", "010101"))

    @pytest.mark.parametrize("bad", ["1", "010", "01010", "0101010", "01a101"])
    def test_bad_codes_rejected(self, bad):
        with pytest.raises(InvalidSpecError):
            ActivitySelector((bad,))

    def test_empty_include_rejected(self):
        with pytest.raises(InvalidSpecError):
            ActivitySelector(())

    def test_presets_exist_and_are_selectors(self):
        for name, preset in ACTIVITY_PRESETS.items():
            assert isinstance(preset, ActivitySelector), name

    def test_unknown_preset(self):
        with pytest.raises(InvalidSpecError, match="Known presets"):
            ActivitySelector.preset("napping")


class TestPopulationFilter:
    def test_inverted_age_range_rejected(self):
        with pytest.raises(InvalidSpecError):
            PopulationFilter(age_min=54, age_max=25)

    def test_bad_sex_rejected(self):
        with pytest.raises(InvalidSpecError):
            PopulationFilter(sex="M")

    def test_bad_region_rejected(self):
        with pytest.raises(InvalidSpecError):
            PopulationFilter(region=9)

    def test_bad_date_window_rejected(self):
        with pytest.raises(InvalidSpecError):
            PopulationFilter(
                diary_date_min=date(2020, 6, 1), diary_date_max=date(2020, 1, 1)
            )

    def test_describe_unrestricted_names_the_universe(self):
        assert "civilian noninstitutional" in PopulationFilter().describe()


class TestAnalysisSpec:
    def test_empty_years_rejected(self):
        with pytest.raises(InvalidSpecError):
            make_spec(years=())

    def test_pre_atus_year_rejected(self):
        with pytest.raises(InvalidSpecError, match="2003"):
            make_spec(years=(1999,))

    def test_duplicate_years_rejected(self):
        with pytest.raises(InvalidSpecError):
            make_spec(years=(2024, 2024))

    def test_bad_weights_rejected(self):
        with pytest.raises(InvalidSpecError):
            make_spec(weights="tufinlwgt")

    def test_bad_variance_rejected(self):
        with pytest.raises(InvalidSpecError):
            make_spec(variance="bootstrap")

    def test_roundtrip_serialization(self):
        spec = make_spec(
            years=(2019, 2021),
            population=PopulationFilter(
                age_min=25, age_max=54, sex="female",
                diary_date_min=date(2019, 5, 10),
            ),
            activity=ActivitySelector(("02", "1802"), exclude=("020903",), label="HH"),
        )
        restored = AnalysisSpec.from_dict(spec.to_dict())
        assert restored == spec

    def test_from_dict_accepts_preset_name(self):
        spec = AnalysisSpec.from_dict(
            {"measure": "participation_rate", "activity": "sleep", "years": [2025]}
        )
        assert spec.activity == ACTIVITY_PRESETS["sleep"]

    def test_from_dict_rejects_unknown_population_fields(self):
        with pytest.raises(InvalidSpecError, match="Unknown population fields"):
            AnalysisSpec.from_dict(
                {
                    "measure": "participation_rate", "activity": "sleep",
                    "years": [2025], "population": {"favourite_colour": "blue"},
                }
            )


class TestComparisonSpec:
    def test_groups_merge_with_shared_population(self):
        comparison = ComparisonSpec(
            base=make_spec(population=PopulationFilter(age_min=25, age_max=54)),
            group_a=PopulationFilter(sex="male"),
            group_b=PopulationFilter(sex="female"),
        )
        spec_a = comparison.spec_for("a")
        assert spec_a.population.sex == "male"
        assert spec_a.population.age_min == 25

    def test_conflicting_group_filter_rejected(self):
        comparison = ComparisonSpec(
            base=make_spec(population=PopulationFilter(sex="male")),
            group_a=PopulationFilter(sex="female"),
            group_b=PopulationFilter(),
        )
        with pytest.raises(InvalidSpecError, match="conflicts"):
            comparison.spec_for("a")

    def test_age_bounds_merge_to_more_restrictive(self):
        merged = merge_filters(
            PopulationFilter(age_min=25, age_max=54),
            PopulationFilter(age_min=35, age_max=44),
        )
        assert (merged.age_min, merged.age_max) == (35, 44)
