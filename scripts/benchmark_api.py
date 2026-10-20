#!/usr/bin/env python3
"""Phase 3 API performance baseline (in-process TestClient, full database).

Measures representative endpoints uncached and cached, demonstrating that the
result cache addresses the expensive replicate-variance requests. The years
come from GET /api/v1/meta, so the cases follow a new release automatically.
Run from the repo root with the database loaded:

    python scripts/benchmark_api.py
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from atus_pipeline.api.app import create_app
from atus_pipeline.config import load_settings


def cases(years: list[int]) -> list[tuple[str, str, str, dict | None]]:
    """The benchmark requests for the survey years /meta reports."""
    latest = max(years)
    pooled = [y for y in years if y != 2020]  # 2020 has no multi-year weight
    return [
        ("GET /api/v1/meta", "GET", "/api/v1/meta", None),
        ("GET /api/v1/activities (full lexicon)", "GET", "/api/v1/activities", None),
        (
            f"estimate: sleep {latest}, variance=none", "POST", "/api/v1/analysis/estimate",
            {"activity": {"preset": "sleep"}, "years": [latest], "variance": "none"},
        ),
        (
            f"estimate: sleep {latest}, replicate SE", "POST", "/api/v1/analysis/estimate",
            {"activity": {"preset": "sleep"}, "years": [latest]},
        ),
        (
            f"estimate: pooled {len(pooled)} years, replicate SE", "POST",
            "/api/v1/analysis/estimate",
            {"activity": {"preset": "sleep"}, "years": pooled},
        ),
        (
            f"trend: sleep {years[0]}-{latest}, per-year SEs", "POST",
            "/api/v1/analysis/trend",
            {"activity": {"preset": "sleep"}, "years": list(years)},
        ),
        (
            f"compare: men vs women household {latest}", "POST", "/api/v1/analysis/compare",
            {"activity": {"preset": "household_activities_bls_table"}, "years": [latest],
             "group_a": {"sex": "male"}, "group_b": {"sex": "female"}},
        ),
    ]


def measure(client: TestClient, method: str, path: str, body: dict | None) -> tuple[float, str]:
    start = time.perf_counter()
    if method == "GET":
        response = client.get(path)
    else:
        response = client.post(path, json=body)
    elapsed = time.perf_counter() - start
    response.raise_for_status()
    return elapsed, response.headers.get("X-Cache", "-")


def main() -> None:
    app = create_app(load_settings())
    with TestClient(app) as client:
        meta = client.get("/api/v1/meta")
        meta.raise_for_status()
        years = meta.json()["data"]["years"]
        print(f"{'case':<44} {'first (s)':>10} {'repeat (s)':>11} {'cache':>6}")
        for name, method, path, body in cases(years):
            first, _ = measure(client, method, path, body)
            repeat, cache_state = measure(client, method, path, body)
            print(f"{name:<44} {first:>10.3f} {repeat:>11.3f} {cache_state:>6}")


if __name__ == "__main__":
    main()
