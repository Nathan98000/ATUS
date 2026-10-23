#!/usr/bin/env python3
"""Phase 3 API performance baseline (in-process TestClient, full database).

Measures representative endpoints uncached and cached, demonstrating that the
result cache addresses the expensive replicate-variance requests. Run from
the repo root with the database loaded:

    python scripts/benchmark_api.py
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from atus_pipeline.api.app import create_app
from atus_pipeline.config import load_settings

CASES = [
    ("GET /api/v1/meta", "GET", "/api/v1/meta", None),
    ("GET /api/v1/activities (full lexicon)", "GET", "/api/v1/activities", None),
    (
        "estimate: sleep 2025, variance=none", "POST", "/api/v1/analysis/estimate",
        {"activity": {"preset": "sleep"}, "years": [2025], "variance": "none"},
    ),
    (
        "estimate: sleep 2025, replicate SE", "POST", "/api/v1/analysis/estimate",
        {"activity": {"preset": "sleep"}, "years": [2025]},
    ),
    (
        "estimate: pooled 22 years, replicate SE", "POST", "/api/v1/analysis/estimate",
        {"activity": {"preset": "sleep"},
         "years": [y for y in range(2003, 2026) if y != 2020]},
    ),
    (
        "trend: sleep 2003-2025, per-year SEs", "POST", "/api/v1/analysis/trend",
        {"activity": {"preset": "sleep"}, "years": list(range(2003, 2026))},
    ),
    (
        "compare: men vs women household 2025", "POST", "/api/v1/analysis/compare",
        {"activity": {"preset": "household_activities_bls_table"}, "years": [2025],
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
        client.get("/ready").raise_for_status()
        print(f"{'case':<44} {'first (s)':>10} {'repeat (s)':>11} {'cache':>6}")
        for name, method, path, body in CASES:
            first, _ = measure(client, method, path, body)
            repeat, cache_state = measure(client, method, path, body)
            print(f"{name:<44} {first:>10.3f} {repeat:>11.3f} {cache_state:>6}")


if __name__ == "__main__":
    main()
