"""Sanity tests for the source registry and its coupling to the transformers."""

from pathlib import Path

import pytest

from atus_pipeline.sources import (
    Release,
    get_release,
    get_source,
    known_releases,
    lexicon_label,
    lexicon_path,
    multiyear_release,
    register_release,
    release_files,
    unregister_release,
)
from atus_pipeline.transformation.tables import TABLE_SPECS
from atus_pipeline.validation.source_checks import expected_years

REPO_ROOT = Path(__file__).resolve().parents[2]

ROW_COUNTS = {
    "respondent": 7, "roster": 7, "activity": 20, "who": 20, "cps": 7,
    "replicate_weights": 7, "pandemic_weights": 3, "activity_summary": 7,
}


class TestRelease0325:
    """The registry refactor must leave the committed release exactly as it was."""

    def test_span_label_and_lexicon(self):
        release = get_release("0325")
        assert (release.first_year, release.last_year) == (2003, 2025)
        assert release.years == tuple(range(2003, 2026))
        assert release.label == "2003-25"
        assert release.lexicon_file == "activity_lexicon_0325.csv"

    def test_file_names_urls_and_counts(self):
        by_key = {s.key: s for s in release_files("0325")}
        assert by_key["respondent"].url == "https://www.bls.gov/tus/datafiles/atusresp-0325.zip"
        assert by_key["respondent"].dat_name == "atusresp_0325.dat"
        assert by_key["activity"].zip_name == "atusact-0325.zip"
        assert by_key["activity"].expected_rows == 4_994_172
        assert by_key["cps"].expected_column_count == 265
        assert by_key["activity_summary"].expected_column_count == 456
        assert not by_key["activity_summary"].load
        assert by_key["pandemic_weights"].dat_name == "atuswgtspan_1920.dat"
        assert by_key["respondent"].description == (
            "ATUS 2003-25 Respondent file (one record per respondent)"
        )

    def test_every_registered_release_has_a_committed_lexicon(self):
        for code in known_releases():
            assert lexicon_path(REPO_ROOT / "data" / "reference", code).is_file(), code


class TestNewRelease:
    """A new release is one registry entry: nothing else needs a code edit."""

    @pytest.fixture
    def release_0326(self):
        release = multiyear_release(
            "0326", first_year=2003, last_year=2026, expected_rows=ROW_COUNTS
        )
        register_release(release)
        yield release
        unregister_release("0326")

    def test_everything_release_specific_derives_from_the_entry(self, release_0326):
        assert "0326" in known_releases()
        respondent = get_source("0326", "respondent")
        assert respondent.zip_name == "atusresp-0326.zip"
        assert respondent.dat_name == "atusresp_0326.dat"
        assert respondent.url.endswith("/atusresp-0326.zip")
        assert respondent.description.startswith("ATUS 2003-26 ")
        assert respondent.expected_rows == 7
        assert expected_years("0326") == set(range(2003, 2027))
        assert lexicon_path(Path("ref"), "0326") == Path("ref/activity_lexicon_0326.csv")
        assert lexicon_label("0326") == "2003-26"
        # the pandemic file is a fixed 2019-20 span in every release
        assert get_source("0326", "pandemic_weights").dat_name == "atuswgtspan_1920.dat"

    def test_new_release_keeps_the_transformer_coupling(self, release_0326):
        for spec in TABLE_SPECS:
            assert get_source("0326", spec.dataset_key).load

    def test_registering_twice_is_refused(self, release_0326):
        with pytest.raises(ValueError, match="already registered"):
            register_release(release_0326)

    def test_code_must_match_the_year_span(self):
        with pytest.raises(ValueError, match="does not match"):
            Release(code="0326", first_year=2003, last_year=2025, files=())
        with pytest.raises(ValueError, match="does not match"):
            multiyear_release("0326", first_year=2004, last_year=2026, expected_rows=ROW_COUNTS)

    def test_wide_file_layouts_can_change_between_releases(self):
        release = multiyear_release(
            "0326", first_year=2003, last_year=2026, expected_rows=ROW_COUNTS,
            cps_column_count=270, summary_column_count=460,
        )
        by_key = {s.key: s for s in release.files}
        assert by_key["cps"].expected_column_count == 270
        assert by_key["activity_summary"].expected_column_count == 460


class TestLexiconLabel:
    def test_registered_release(self):
        assert lexicon_label("0325") == "2003-25"

    def test_unregistered_but_well_formed_code(self):
        # a database loaded by a newer pipeline still gets a correct label
        assert lexicon_label("0327") == "2003-27"

    @pytest.mark.parametrize("release", [None, "unloaded", "9903", "03", "abcd"])
    def test_anything_else_has_no_label(self, release):
        assert lexicon_label(release) is None


class TestRegistry:
    def test_release_0325_exists(self):
        assert len(release_files("0325")) == 8

    def test_unknown_release_raises(self):
        with pytest.raises(KeyError, match="Unknown ATUS release"):
            release_files("9999")

    def test_keys_are_unique(self):
        keys = [s.key for s in release_files("0325")]
        assert len(keys) == len(set(keys))

    def test_urls_are_official_bls(self):
        for source in release_files("0325"):
            assert source.url.startswith("https://www.bls.gov/tus/"), source.key

    def test_every_source_validates_header_somehow(self):
        for source in release_files("0325"):
            assert source.expected_columns or source.expected_column_count, source.key

    def test_official_row_counts_recorded(self):
        for source in release_files("0325"):
            assert source.expected_rows > 0, source.key


class TestSpecSourceCoupling:
    def test_every_table_spec_has_a_source(self):
        for spec in TABLE_SPECS:
            source = get_source("0325", spec.dataset_key)
            assert source.load, f"{spec.table} maps to a non-loaded source"

    def test_loaded_sources_all_have_table_specs(self):
        spec_keys = {spec.dataset_key for spec in TABLE_SPECS}
        for source in release_files("0325"):
            if source.load:
                assert source.key in spec_keys, source.key
            else:
                assert source.key not in spec_keys, source.key

    def test_transformers_only_read_registered_columns(self):
        """A transformer must not depend on columns the header validation
        does not guarantee (full layouts or required_columns)."""
        from tests import fixtures

        rows = {
            "respondent": fixtures.respondent_row(),
            "roster": fixtures.roster_row(),
            "activity": fixtures.activity_row(),
            "who": fixtures.who_row(),
            "cps": fixtures.cps_row(),
            "replicate_weights": fixtures.replicate_weights_row(),
            "pandemic_weights": fixtures.pandemic_weights_row(),
        }
        for spec in TABLE_SPECS:
            spec.transform(rows[spec.dataset_key])  # KeyError = unregistered column
