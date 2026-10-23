# Phase 5 baseline (commit 387fc2d, full local DB, 2026-10-20)

## Backend lint/unit/integration
```
Tue Oct 20 16:59:16 UTC 2026
All checks passed!
ruff exit 0
  $REPO/.venv/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
194 passed, 1 warning in 0.50s
pytest tests/unit -q 2>&1  0.54s user 0.09s system 78% cpu 0.813 total
tail -5  0.00s user 0.00s system 0% cpu 0.813 total
  $REPO/.venv/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
50 passed, 1 warning in 11.11s
pytest tests/integration -q 2>&1  0.56s user 0.33s system 7% cpu 11.386 total
tail -5  0.00s user 0.00s system 0% cpu 11.386 total
```

## Full database: atus validate-analytics + ATUS_DATA_QUALITY=1 pytest tests/data_quality
```
Tue Oct 20 16:59:36 UTC 2026
== atus validate-analytics
  [PASS] analytics: UG ch7.4: TV watching 2007, mean minutes/day                           157.3948 (published 157.3950, |Δ|=0.00025)
  [PASS] analytics: UG ch7.5: TV watching 2007, replicate-weight SE (hours)                0.0293 (published 0.0293, |Δ|=0.00000)
  [PASS] analytics: UG App J: persons in the South doing housework per day, 2006           29,517,002.7693 (published 29,517,003.0000, |Δ|=0.23067)
  [PASS] analytics: UG App J: unweighted participant count in that subpopulation           1,861.0000 (published 1,861.0000, |Δ|=0.00000)
  [PASS] analytics: A1-2025: sleeping, hours/day                                           9.0327 (published 9.0300, |Δ|=0.00274)
  [PASS] analytics: A1-2025: sleeping, percent participating                               99.8970 (published 99.9000, |Δ|=0.00304)
  [PASS] analytics: A1-2025: sleeping, hours/day of participants                           9.0421 (published 9.0400, |Δ|=0.00205)
  [PASS] analytics: A1-2025: sleeping, men, hours/day                                      8.9298 (published 8.9300, |Δ|=0.00017)
  [PASS] analytics: A1-2025: sleeping, women, hours/day                                    9.1308 (published 9.1300, |Δ|=0.00080)
  [PASS] analytics: A1-2025: watching TV, hours/day                                        2.6105 (published 2.6100, |Δ|=0.00054)
  [PASS] analytics: A1-2025: watching TV, percent participating                            74.3346 (published 74.3000, |Δ|=0.03458)
  [PASS] analytics: A1-2025: household activities (A-1 definition), hours/day              1.9926 (published 1.9900, |Δ|=0.00264)
  [PASS] analytics: A1-2025: household activities (A-1 definition), percent participating  80.9453 (published 80.9000, |Δ|=0.04529)
  [PASS] analytics: A1-2025: household activities (A-1 definition), women, hours/day       2.3842 (published 2.3800, |Δ|=0.00419)
  [PASS] analytics: A1-2025: leisure and sports, hours/day                                 5.1607 (published 5.1600, |Δ|=0.00067)
  [PASS] analytics: A1-2025: leisure and sports, percent participating                     95.1125 (published 95.1000, |Δ|=0.01251)
  [PASS] analytics: A1-2025: leisure and sports, hours/day of participants                 5.4259 (published 5.4300, |Δ|=0.00414)
  [PASS] analytics: A1-2025: eating and drinking, hours/day                                1.2122 (published 1.2100, |Δ|=0.00216)
  [PASS] analytics: A1-2025: eating and drinking, percent participating                    96.8794 (published 96.9000, |Δ|=0.02063)
  [PASS] analytics: A1-2025: working and work-related, hours/day                           3.3159 (published 3.3200, |Δ|=0.00411)
  [PASS] analytics: A1-2025: working and work-related, hours/day of participants           7.9847 (published 7.9800, |Δ|=0.00466)
  [PASS] analytics: NR-2020: sleeping, hours/day (May 10 - Dec 31, TU20FWGT)               9.0088 (published 9.0100, |Δ|=0.00120)
  [PASS] analytics: NR-2020: watching TV, hours/day (May 10 - Dec 31, TU20FWGT)            3.0548 (published 3.0500, |Δ|=0.00476)
  [PASS] analytics: NR-2020: watching TV, percent participating (May 10 - Dec 31)          78.7977 (published 78.8000, |Δ|=0.00231)

24 passed, 0 failed, 0 warnings
atus validate-analytics  0.11s user 0.02s system 1% cpu 10.884 total
exit 0
== data_quality
..............                                                           [100%]
=============================== warnings summary ===============================
.venv/lib/python3.12/site-packages/fastapi/testclient.py:1
  $REPO/.venv/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
==================================== PASSES ====================================
=========================== short test summary info ============================
PASSED tests/data_quality/test_analytics_benchmarks.py::test_engine_reproduces_official_bls_estimates
PASSED tests/data_quality/test_analytics_benchmarks.py::test_full_trend_runs_and_flags_2020
PASSED tests/data_quality/test_api_benchmarks.py::test_bls_benchmark_over_http[UG ch7.4: TV watching 2007, mean minutes/day]
PASSED tests/data_quality/test_api_benchmarks.py::test_bls_benchmark_over_http[UG ch7.5: TV watching 2007, replicate-weight SE (hours)]
PASSED tests/data_quality/test_api_benchmarks.py::test_bls_benchmark_over_http[UG App J: persons in the South doing housework per day, 2006]
PASSED tests/data_quality/test_api_benchmarks.py::test_bls_benchmark_over_http[A1-2025: sleeping, hours/day]
PASSED tests/data_quality/test_api_benchmarks.py::test_bls_benchmark_over_http[A1-2025: sleeping, women, hours/day]
PASSED tests/data_quality/test_api_benchmarks.py::test_bls_benchmark_over_http[A1-2025: watching TV, percent participating]
PASSED tests/data_quality/test_api_benchmarks.py::test_bls_benchmark_over_http[NR-2020: sleeping, hours/day (May 10 - Dec 31, TU20FWGT)]
PASSED tests/data_quality/test_api_benchmarks.py::test_trend_over_http_full_period
PASSED tests/data_quality/test_api_benchmarks.py::test_compare_over_http_matches_published_by_sex_values
PASSED tests/data_quality/test_full_database.py::test_validation_suite_has_no_errors
PASSED tests/data_quality/test_full_database.py::test_weighted_sleep_estimate_is_plausible
PASSED tests/data_quality/test_full_database.py::test_2020_partial_year_has_two_windows
14 passed, 1 warning in 34.75s
ATUS_DATA_QUALITY=1 pytest tests/data_quality -q -rA 2>&1  0.60s user 0.09s system 1% cpu 35.000 total
tail -40  0.00s user 0.00s system 0% cpu 35.000 total
```

## Frontend: npm ci, typecheck, lint, format, vitest, build
```
npm warn install-scripts   msw@2.15.0 (postinstall: node -e "import('./config/scripts/postinstall.js').catch(() => void 0)")
npm warn install-scripts
npm warn install-scripts Run `npm install-scripts ls` to review, or `npm install-scripts approve <pkg>` to allow.
> atus-explorer@0.1.0 typecheck
> tsc -b

src/features/results/EstimateView.tsx:21:17: warning react(only-export-components): Fast refresh only works when a file only exports components. Use a new file to share constants or functions between components.
src/features/results/TrendView.tsx:25:17: warning react(only-export-components): Fast refresh only works when a file only exports components. Use a new file to share constants or functions between components.
src/features/results/TrendChart.tsx:49:17: warning react(only-export-components): Fast refresh only works when a file only exports components. Use a new file to share constants or functions between components.

Checking formatting...
All matched files use Prettier code style!
      Tests  119 passed (119)
   Start at  12:59:41
   Duration  5.63s (tests 39%, environment 38%, setup 12%, transform 9%, import 2%)

Environment  jsdom was created 13 times · 8.83s total, 38% of tracked time
             create it once per worker with pool: 'vmThreads' (keeps per-file isolation) or isolate: false (shares it across files)
             learn more: https://vitest.dev/guide/improving-performance#test-environments

> tsc -b && vite build

vite v8.2.2 building client environment for production...
transforming...
✓ 152 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   0.64 kB │ gzip:   0.39 kB
dist/assets/index-iJKMLZcZ.css   14.97 kB │ gzip:   3.82 kB
dist/assets/index-DaQ2KCZt.js   365.00 kB │ gzip: 111.85 kB

✓ built in 185ms
```

## Frontend E2E (Playwright, CI=1)
```

Running 20 tests using 5 workers

  ✓   2 [desktop] › e2e/accessibility.spec.ts:21:1 › home page has no serious axe violations (873ms)
  ✓   4 [desktop] › e2e/accessibility.spec.ts:59:1 › the primary workflow is keyboard-operable (960ms)
  ✓   1 [desktop] › e2e/compare.spec.ts:7:1 › configure, run, understand and reshare a comparison (1.2s)
  ✓   3 [desktop] › e2e/accessibility.spec.ts:27:1 › explorer has no serious axe violations (1.2s)
  ✓   7 [desktop] › e2e/errors.spec.ts:28:1 › an unknown activity code is refused with the lexicon explanation (273ms)
  ✓   6 [desktop] › e2e/errors.spec.ts:19:1 › the 2020 multiyear restriction shows the API explanation (345ms)
  ✓   8 [desktop] › e2e/errors.spec.ts:34:1 › an analytically invalid population is a clear request problem (315ms)
  ✓   9 [desktop] › e2e/errors.spec.ts:47:1 › an empty population is explained, not rendered as zero (432ms)
  ✓  10 [desktop] › e2e/errors.spec.ts:61:1 › a down backend is presented as unavailability with retry, then recovers (478ms)
  ✓  11 [desktop] › e2e/explorer.spec.ts:9:1 › a first-time user completes an estimate from the home page (803ms)
  ✓  13 [desktop] › e2e/explorer.spec.ts:65:1 › an estimate can be viewed as a trend across all available years (548ms)
  ✓  16 [desktop] › e2e/share.spec.ts:57:1 › an invalid share link gets a helpful page, not a blank screen (303ms)
  ✓  15 [desktop] › e2e/share.spec.ts:44:1 › refreshing a result page reproduces the same analysis (539ms)
  ✓  12 [desktop] › e2e/explorer.spec.ts:52:1 › the loading state announces the calculation (1.2s)
  ✓   5 [desktop] › e2e/accessibility.spec.ts:33:1 › estimate, trend and compare results have no serious axe violations (2.9s)
  ✓  14 [desktop] › e2e/share.spec.ts:9:1 › a shared analysis URL reproduces the analysis in a fresh browser context (1.2s)
  ✓  17 [desktop] › e2e/trend-2020.spec.ts:7:1 › 2020 renders as an explicit, explained gap in trends (476ms)
  ✓  18 [desktop] › e2e/trend-2020.spec.ts:42:1 › the chart is keyboard-inspectable, including the unavailable year (435ms)
  ✓  20 [mobile] › e2e/mobile.spec.ts:40:1 › comparison groups stack vertically and stay readable (437ms)
  ✓  19 [mobile] › e2e/mobile.spec.ts:7:1 › the core workflow works on a phone without horizontal scrolling (591ms)

  20 passed (8.6s)
exit 0
```
