"""Sanity tests for the source registry and its coupling to the transformers."""

import pytest

from atus_pipeline.sources import get_source, release_files
from atus_pipeline.transformation.tables import TABLE_SPECS


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
