"""Phase 2 BLS benchmarks replayed through the HTTP stack (opt-in).

Sends representative benchmark specifications through
HTTP -> FastAPI -> validation -> AnalysisEngine -> PostgreSQL -> serialization
and checks the same published values Phase 2 verifies engine-side. The specs
are taken directly from the Phase 2 benchmark registry (their canonical
``to_dict()`` form *is* a valid API request body — the reproducibility
contract), so expected values and tolerances cannot drift between suites.

    ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_api_benchmarks.py
"""

from __future__ import annotations

import os

import pytest

from atus_pipeline.config import load_settings

pytestmark = pytest.mark.data_quality

if not os.environ.get("ATUS_DATA_QUALITY"):
    pytest.skip(
        "set ATUS_DATA_QUALITY=1 to replay BLS benchmarks through the API",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient  # noqa: E402

from atus_pipeline.analytics import benchmarks as bm  # noqa: E402
from atus_pipeline.api.app import create_app  # noqa: E402

# Representative subset of the 24-benchmark suite (kept small for runtime):
# guide worked examples (mean + SE + subgroup persons/day), a published table
# value, a participation rate, a by-sex subgroup, and a pandemic-weight value.
_REPLAYED = (
    "UG ch7.4: TV watching 2007, mean minutes/day",
    "UG ch7.5: TV watching 2007, replicate-weight SE (hours)",
    "UG App J: persons in the South doing housework per day, 2006",
    "A1-2025: sleeping, hours/day",
    "A1-2025: sleeping, women, hours/day",
    "A1-2025: watching TV, percent participating",
    "NR-2020: sleeping, hours/day (May 10 - Dec 31, TU20FWGT)",
)

# Same extraction semantics as the engine-side suite, applied to JSON payloads.
_JSON_EXTRACTORS = {
    bm._value: lambda p: p["estimate"]["value"],
    bm._mean_hours: lambda p: p["estimate"]["value"] / 60.0,
    bm._se_hours: lambda p: p["estimate"]["standard_error"] / 60.0,
    bm._participation_pct: lambda p: p["estimate"]["value"] * 100.0,
    bm._sample_participants: lambda p: float(p["n_participants"]),
}


@pytest.fixture(scope="module")
def client():
    app = create_app(load_settings())
    with TestClient(app) as test_client:
        yield test_client


def _selected_benchmarks():
    by_name = {b.name: b for b in bm.build_benchmarks()}
    return [by_name[name] for name in _REPLAYED]


@pytest.mark.parametrize("benchmark", _selected_benchmarks(), ids=lambda b: b.name)
def test_bls_benchmark_over_http(client, benchmark):
    body = benchmark.spec.to_dict()
    response = client.post("/api/v1/analysis/estimate", json=body)
    assert response.status_code == 200, response.text
    observed = _JSON_EXTRACTORS[benchmark.extract](response.json())
    assert observed == pytest.approx(benchmark.expected, abs=benchmark.tolerance), (
        f"{benchmark.name}: observed {observed}, published {benchmark.expected} "
        f"({benchmark.source})"
    )


def test_trend_over_http_full_period(client):
    body = {
        "activity": {"preset": "sleep"},
        "years": list(range(2003, 2026)),
        "variance": "none",
    }
    response = client.post("/api/v1/analysis/trend", json=body)
    assert response.status_code == 200
    points = {p["year"]: p for p in response.json()["points"]}
    assert len(points) == 23
    assert points[2020]["estimate"] is None
    for year, point in points.items():
        if year != 2020:
            assert 480 <= point["estimate"]["value"] <= 600, year


def test_compare_over_http_matches_published_by_sex_values(client):
    body = {
        "activity": {"preset": "household_activities_bls_table"},
        "years": [2025],
        "group_a": {"sex": "male"},
        "group_b": {"sex": "female"},
        "label_a": "Men", "label_b": "Women",
    }
    response = client.post("/api/v1/analysis/compare", json=body)
    assert response.status_code == 200
    payload = response.json()
    # BLS Table A-1 2025: men 1.58 h/day, women 2.38 h/day
    assert payload["group_a"]["estimate"]["value"] / 60 == pytest.approx(1.58, abs=0.006)
    assert payload["group_b"]["estimate"]["value"] / 60 == pytest.approx(2.38, abs=0.006)
    assert payload["difference"]["standard_error"] is not None
