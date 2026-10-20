"""Unit tests for the analysis-result cache and its deterministic keys."""

import pytest

from atus_pipeline.api.cache import InMemoryAnalysisCache, NullAnalysisCache, cache_key

SPEC = {
    "measure": "average_minutes_per_day",
    "activity": {"include": ["0101"], "exclude": [], "label": None},
    "years": [2024, 2025],
    "population": {},
    "weights": "multiyear",
    "variance": "replicate",
    "confidence_level": 0.95,
}


class TestCacheKey:
    def test_deterministic(self):
        assert cache_key("estimate", SPEC, "0.1", "0325:run1") == \
            cache_key("estimate", dict(SPEC), "0.1", "0325:run1")

    def test_key_ignores_dict_insertion_order(self):
        reordered = dict(reversed(list(SPEC.items())))
        assert cache_key("estimate", SPEC, "0.1", "0325:run1") == \
            cache_key("estimate", reordered, "0.1", "0325:run1")

    def test_operation_separates_keys(self):
        assert cache_key("estimate", SPEC, "0.1", "0325:run1") != \
            cache_key("trend", SPEC, "0.1", "0325:run1")

    def test_spec_change_separates_keys(self):
        other = {**SPEC, "years": [2025]}
        assert cache_key("estimate", SPEC, "0.1", "0325:run1") != \
            cache_key("estimate", other, "0.1", "0325:run1")

    def test_analytics_version_invalidates(self):
        assert cache_key("estimate", SPEC, "0.1", "0325:run1") != \
            cache_key("estimate", SPEC, "0.2", "0325:run1")

    def test_data_version_invalidates(self):
        assert cache_key("estimate", SPEC, "0.1", "0325:run1") != \
            cache_key("estimate", SPEC, "0.1", "0325:run2")


class TestInMemoryCache:
    def test_miss_then_hit(self):
        cache = InMemoryAnalysisCache(maxsize=4)
        assert cache.get("k") is None
        cache.put("k", {"v": 1})
        assert cache.get("k") == {"v": 1}
        assert cache.hits == 1 and cache.misses == 1

    def test_lru_eviction(self):
        cache = InMemoryAnalysisCache(maxsize=2)
        cache.put("a", {"v": "a"})
        cache.put("b", {"v": "b"})
        cache.get("a")            # refresh a → b is now least-recent
        cache.put("c", {"v": "c"})
        assert cache.get("b") is None
        assert cache.get("a") == {"v": "a"}
        assert cache.get("c") == {"v": "c"}

    def test_maxsize_validated(self):
        with pytest.raises(ValueError):
            InMemoryAnalysisCache(maxsize=0)

    def test_null_cache_never_stores(self):
        cache = NullAnalysisCache()
        cache.put("k", {"v": 1})
        assert cache.get("k") is None
