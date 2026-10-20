"""Year pushdown (F39) changes no result: the Phase 2-4 query and the current
one return the same counts and exactly the same ``numeric`` sums, and
replicate ``float8`` sums within the §9 tolerance, over the audit's extreme
specifications and a seeded random sample, on the randomized dataset and on
the hand-computed fixture; the engine's outputs compare equivalent too.

(The same check ran read-only on the full database over 71 real
specifications: docs/adr/ADR-013-data-releases-and-serving-db.md.)
"""

from __future__ import annotations

import random

import psycopg
import pytest

from atus_pipeline.analytics import AnalysisEngine, queries
from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.analytics.errors import AnalyticsError
from atus_pipeline.loading.loader import load_all
from atus_pipeline.serving.equivalence import (
    compare_results,
    compare_statistics,
    random_specs,
    rendered_queries,
    run,
    seed_specs,
)
from tests.integration.analytics_dataset import analytics_settings
from tests.integration.conftest import create_scratch_database, drop_scratch_database
from tests.integration.random_dataset import random_settings

pytestmark = pytest.mark.integration


def _loaded(test_db_url, tmp_path_factory, suffix, make_settings):
    url = create_scratch_database(test_db_url, suffix)
    settings = make_settings(tmp_path_factory.mktemp(suffix), url)
    with psycopg.connect(url) as conn:
        load_all(conn, settings)
    return url


@pytest.fixture(scope="module", params=["random", "handmade"])
def database(request, test_db_url, tmp_path_factory):
    make = random_settings if request.param == "random" else analytics_settings
    url = _loaded(test_db_url, tmp_path_factory, f"pushdown_{request.param}", make)
    yield url
    drop_scratch_database(url)


def _specs(conn, n: int, seed: int):
    years = [y for (y,) in conn.execute(
        "SELECT DISTINCT data_year FROM atus.respondents ORDER BY 1")]
    tier1 = [c for (c,) in conn.execute("SELECT code FROM atus.activity_tier1 ORDER BY 1")]
    leaves = [c for (c,) in conn.execute(
        "SELECT DISTINCT activity_code FROM atus.activities ORDER BY 1")]
    return years, seed_specs(years, tier1) + random_specs(
        random.Random(seed), years=years, tier1=tier1, leaves=leaves, n=n)


def test_raw_statistics_are_equivalent(database):
    compared = 0
    with psycopg.connect(database, autocommit=True) as conn:
        years, specs = _specs(conn, n=250, seed=11)
        resolver = ActivityResolver(conn)
        for _, spec in specs:
            try:
                pairs = rendered_queries(conn, resolver, spec, years_loaded=tuple(years))
            except AnalyticsError:
                continue
            for old_sql, new_sql, params in pairs:
                assert old_sql != new_sql
                old = conn.execute(old_sql, params).fetchall()
                new = conn.execute(new_sql, params).fetchall()
                result = compare_statistics(old, new)
                assert result.equivalent, (spec, result.mismatches[:3])
                compared += 1
    assert compared >= 250


def test_engine_results_are_equivalent(database, monkeypatch):
    with psycopg.connect(database, autocommit=True) as conn:
        _, specs = _specs(conn, n=120, seed=12)
        current = [run(AnalysisEngine(conn), op, spec) for op, spec in specs]
        monkeypatch.setattr(queries, "_YEAR_PUSHDOWN", "")       # the Phase 2-4 query
        legacy = [run(AnalysisEngine(conn), op, spec) for op, spec in specs]
    assert sum(isinstance(r, dict) for r in current) >= 60    # the rest: identical refusals
    for (op, spec), a, b in zip(specs, legacy, current, strict=True):
        result = compare_results(a, b)
        assert result.equivalent, (op, spec, result.mismatches[:3])
