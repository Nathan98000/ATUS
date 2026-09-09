"""End-to-end analytics tests against a real PostgreSQL with hand-computed
expectations (dataset documented in analytics_dataset.py).

Derivations used below (sleep, 2023, respondents E1/E2/E3 with weights
1, 1, 2 and minutes 100, 200, 0):

* mean            = (1·100 + 1·200 + 2·0) / 4 = 75
* participation   = (1 + 1) / 4 = 0.5
* per participant = 300 / 2 = 150
* replicate means: r1 weights (2,0,2) → 200/4 = 50;
  r2 weights (1,2,2) → 500/5 = 100; r3..r160 weights (1,1,2) → 75.
  Var = (4/160)·((50−75)² + (100−75)²) = 31.25 → SE = 5.590169…
* men (E1) vs women (E2,E3): 100 vs 200/3; per-replicate differences
  r1: 100−0 = 100, r2: 100−100 = 0, rest: 100−66.666… = 33.333…;
  Var(diff) = (4/160)·(66.667² + 33.333²) = 138.888… → SE = 11.785113…
"""

from __future__ import annotations

import json

import psycopg
import pytest

from atus_pipeline.analytics import (
    ActivitySelector,
    AnalysisEngine,
    AnalysisSpec,
    ComparisonSpec,
    InsufficientDataError,
    InvalidSpecError,
    Measure,
    PopulationFilter,
    UnknownActivityError,
    UnsupportedAnalysisError,
)
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings

pytestmark = pytest.mark.integration

SLEEP = ActivitySelector(("010101",), label="Sleeping")


@pytest.fixture(scope="module")
def engine(tmp_path_factory, migrated_db):
    settings = analytics_settings(tmp_path_factory.mktemp("analytics"), migrated_db)
    with psycopg.connect(settings.database_url) as conn:
        load_all(conn, settings)
        yield AnalysisEngine(conn)


def spec(**overrides) -> AnalysisSpec:
    defaults = dict(
        measure=Measure.AVERAGE_MINUTES_PER_DAY,
        activity=SLEEP,
        years=(2023,),
    )
    defaults.update(overrides)
    return AnalysisSpec(**defaults)


class TestGrainSafety:
    def test_ten_episodes_count_as_one_respondent(self, engine):
        """E2's 200 sleep minutes arrive in 10 episodes; the weighted mean must
        equal the value obtained if they were a single episode."""
        result = engine.estimate(spec())
        assert result.estimate.value == pytest.approx(75.0)
        assert result.n_respondents == 3          # E1, E2, E3 — exactly once each
        assert result.n_participants == 2

    def test_weighted_population_counts_each_respondent_once(self, engine):
        result = engine.estimate(spec())
        # Σw / D = 4 person-days / 365 days
        assert result.weighted_population_per_day == pytest.approx(4 / 365)


class TestEstimators:
    def test_participation_rate_and_zero_minutes(self, engine):
        """E3 has a sleep episode of 0 minutes — that is not participation."""
        result = engine.estimate(spec(measure=Measure.PARTICIPATION_RATE))
        assert result.estimate.value == pytest.approx(0.5)

    def test_per_participant_mean(self, engine):
        result = engine.estimate(spec(measure=Measure.AVERAGE_MINUTES_PER_PARTICIPANT))
        assert result.estimate.value == pytest.approx(150.0)

    def test_participants_per_day_uses_person_day_denominator(self, engine):
        result = engine.estimate(spec(measure=Measure.PARTICIPANTS_PER_DAY))
        assert result.estimate.value == pytest.approx(2 / 365)
        assert result.days_in_period == 365


class TestReplicateVariance:
    def test_hand_computed_standard_error(self, engine):
        result = engine.estimate(spec())
        assert result.estimate.standard_error == pytest.approx(5.5901699, abs=1e-6)
        z = 1.959964
        assert result.estimate.ci_lower == pytest.approx(75 - z * 5.5901699, abs=1e-4)
        assert result.estimate.ci_upper == pytest.approx(75 + z * 5.5901699, abs=1e-4)

    def test_variance_none_skips_se(self, engine):
        result = engine.estimate(spec(variance="none"))
        assert result.estimate.value == pytest.approx(75.0)
        assert result.estimate.standard_error is None


class TestPopulationSemantics:
    def test_sex_filter(self, engine):
        result = engine.estimate(spec(population=PopulationFilter(sex="female")))
        assert result.estimate.value == pytest.approx(200 / 3)
        assert result.n_respondents == 2

    def test_missing_education_is_excluded_not_no(self, engine):
        """E2 has NULL CPS education. Filtering on education must exclude E2
        entirely (denominator and numerator), never treat it as 'below'."""
        bachelor = engine.estimate(
            spec(population=PopulationFilter(education_level="bachelor_or_higher"),
                 variance="none")
        )
        assert bachelor.n_respondents == 1            # E1 only
        assert bachelor.estimate.value == pytest.approx(100.0)

        below = engine.estimate(
            spec(population=PopulationFilter(education_level="high_school"),
                 variance="none")
        )
        assert below.n_respondents == 1               # E3 only; E2 is nowhere
        assert below.estimate.value == pytest.approx(0.0)

    def test_empty_population_raises(self, engine):
        with pytest.raises(InsufficientDataError):
            engine.estimate(spec(population=PopulationFilter(age_min=95)))


class TestSchemeRules:
    def test_2020_multiyear_estimate_refused(self, engine):
        with pytest.raises(UnsupportedAnalysisError, match="undefined for 2020"):
            engine.estimate(spec(years=(2020,)))

    def test_pandemic_scheme_outside_2019_2020_refused(self, engine):
        with pytest.raises(UnsupportedAnalysisError, match="only for 2019 and 2020"):
            engine.estimate(spec(years=(2023,), weights="pandemic"))

    def test_pandemic_pooled_2019_2020(self, engine):
        result = engine.estimate(spec(years=(2019, 2020), weights="pandemic"))
        # (3·120 + 1·240) / 4 = 150, over 312 + 313 person-days per person
        assert result.estimate.value == pytest.approx(150.0)
        assert result.days_in_period == 312 + 313
        assert result.estimate.standard_error == pytest.approx(0.0)  # constant replicates
        assert any("Pandemic weights" in w for w in result.warnings)

    def test_trend_marks_2020_unavailable_under_multiyear(self, engine):
        result = engine.trend(spec(years=(2019, 2020)))
        by_year = {p.year: p for p in result.points}
        assert by_year[2019].estimate.value == pytest.approx(120.0)
        assert by_year[2020].estimate is None
        assert "TUFNWGTP" in by_year[2020].unavailable_reason

    def test_unloaded_year_rejected(self, engine):
        with pytest.raises(InvalidSpecError, match="not in the database"):
            engine.estimate(spec(years=(2005,)))


class TestComparison:
    def test_difference_with_covariance_correct_se(self, engine):
        comparison = ComparisonSpec(
            base=spec(),
            group_a=PopulationFilter(sex="male"),
            group_b=PopulationFilter(sex="female"),
            label_a="men", label_b="women",
        )
        result = engine.compare(comparison)
        assert result.group_a.estimate.value == pytest.approx(100.0)
        assert result.group_b.estimate.value == pytest.approx(200 / 3)
        assert result.difference.value == pytest.approx(100 - 200 / 3)
        assert result.difference.standard_error == pytest.approx(11.785113, abs=1e-5)


class TestErrorsAndSerialization:
    def test_unknown_activity_code(self, engine):
        with pytest.raises(UnknownActivityError, match="999999"):
            engine.estimate(spec(activity=ActivitySelector(("999999",))))

    def test_result_serializes_to_json(self, engine):
        result = engine.estimate(spec())
        payload = json.loads(json.dumps(result.to_dict()))
        assert payload["estimate"]["value"] == pytest.approx(75.0)
        assert payload["weight"]["bls_variable"] == "TUFNWGTP"
        assert payload["analytics_version"]
        assert payload["spec"]["activity"]["include"] == ["010101"]

    def test_spec_roundtrip_reproduces_result(self, engine):
        original = spec(population=PopulationFilter(sex="female"))
        restored = AnalysisSpec.from_dict(json.loads(json.dumps(original.to_dict())))
        assert engine.estimate(restored).estimate.value == \
            engine.estimate(original).estimate.value
