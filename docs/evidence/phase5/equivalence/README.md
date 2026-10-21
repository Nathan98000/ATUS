# HTTP equivalence run (§9), 2026-10-21

The 3-way HTTP replay of the generated equivalence corpus, run with `scripts/equivalence`.

**Result: FAIL, with one blocking finding.** 543 of 544 specs and 84 of 84 GET requests passed. No statistic differs in any pair. In the one failure, `population-071`, the Phase 5 API on the full database answered 503 `analysis_timeout` while the other two servers answered 200. The cause is a query-plan regression from the Phase 5 year pushdown; see [Finding](#finding-year-pushdown-plan-regression-blocking).

| File | Content |
|---|---|
| [`summary.md`](summary.md) | The harness's summary of this run, unedited |
| [`report.json`](report.json) | The per-spec report: each target's status and code, and per pair the outcome, values compared, `max_relative`, `max_scaled` and mismatches with JSON paths |
| [`specs/`](specs/) | The 544 replayed specs, byte-identical to the run's input (sha256 in `report.json` → `inputs.spec_files`) |

## Provenance

| Target | Code | Database | Data version |
|---|---|---|---|
| a | Phase 1-4 API, `387fc2d` (detached worktree; venv from `requirements/api.lock.txt` with `--require-hashes`) | full: `atus@localhost:5434/atus` (container `atus-postgres`) | `0325:run1:1788919495952073` |
| b | Phase 5 API, `p5/eq` at `1bb1d6f` (`400cb6f` + the harness; no `src/` change) | full: as a | `0325:run1:1788919495952073` |
| c | as b | serving: WS-B restore drill `atus-wsb-drill` (port 5466), role `atus_api` | `0325:serving-20261020T224252Z-a7420a5c:de834e672474` |

- Both databases run PostgreSQL 16.15. All three servers use fastapi 0.156.2, starlette 1.9.0, pydantic 2.14.1, uvicorn 0.54.0, psycopg 3.3.6 and psycopg-pool 3.3.3.
- Every connection uses `default_transaction_read_only=on`. Nothing was written to either database.
- Specs: 5 generated files with 544 specs (compare-edge 108, errors-weird 110, measures-weights 109, pooled-trends 108, population 109). Of these, 418 reach the database (362 answered 200, 55 `insufficient_data`, and `population-071`); the other 126 are refused before any query.
- Settings: `--jobs 1`, `--retries 3`, 40 sampled activity codes (seed 20261020). The Phase 5 servers run with a 600 s statement timeout; the other settings are listed in `report.json` → `servers.*.settings`.
- Nothing was timed. The harness does not measure durations, and this README quotes none.

Command, run from the `p5/eq` worktree, with `S` set to the session scratch directory:

```sh
ATUS_EQ_BASELINE_DIR=$S/wave2/baseline-387fc2d scripts/equivalence/run.sh \
    --specs $S/wave2/eq-specs --out $S/wave2/eq-run --ports 8661,8662,8663 \
    --serving-secrets-file $S/impl/wsb/drill/neon.env --save-responses all
```

The output was checked before it was copied here. `report.json`, `summary.md` and the three server logs contain no connection string and none of the secrets file's values.

## Results

| Pair | Requests | Values compared | Outcomes | Max scaled SE/CI diff (bound 1e-12) | Max relative SE/CI diff |
|---|---|---|---|---|---|
| a vs b (Phase 1-4 vs Phase 5 code, full DB) | 628 | 34,301 | 620 equivalent, 7 intended difference, 1 mismatch | 7.03e-15 (`pooled-trends-072`) | 0.691 (`pooled-trends-072`) |
| b vs c (full vs serving DB, Phase 5 code) | 628 | 34,303 | 627 equivalent, 1 mismatch | 4.02e-15 (`pooled-trends-046`) | 0.495 (`errors-weird-102`) |
| a vs c (extra transitivity check over the saved answers) | 544 specs | | 544 equivalent | 7.25e-15 (`pooled-trends-072`) | 0.75 (`compare-edge-036`) |

- In both pairs, the only mismatch is the status of `population-071`. Its b-vs-c header differences (`cache-control`, `x-analytics-version`, `x-response-format`) follow from b answering with an error.
- The largest relative differences come from zero-variance estimates. There the "standard error" is floating-point noise around 0, for example a difference of 0 between identical groups in `errors-weird-102`: SE 1.30e-15 on b vs 2.56e-15 on c. The scale floor of the criterion (F40) handles these; the largest scaled difference is 7.03e-15.
- Intended a-vs-b differences occurred on GET requests only: `lexicon_wording` (5), `control_characters_rejected` (1) and `health_reports_build` (1). No POST spec needed one. No whitelisted body value differed.
- Expectations: b met every spec's expected outcome except `population-071` (expected `ok`).
- a vs c for `population-071`: equivalent, 32 values compared, max scaled 9.45e-16. The statistic itself is not in question; only b's ability to answer it is.

## Finding: year-pushdown plan regression (blocking)

**What fails.** `population-071` is a participants-per-day estimate of `eating_and_drinking` for 2019 under the pandemic weights, with diary dates 2019-05-10 to 2019-12-31. The Phase 5 API on the full database answered 503 `analysis_timeout` after its 600 s statement timeout. The production default is 85 s (`api_statement_timeout_ms`), so a production server on that database would refuse it too.

`measures-weights-043` has the same plan and population size (watching TV, 2020, pandemic weights, 2020-05-10 to 2020-12-31). It hit the 600 s timeout in the harness author's earlier corpus run (`--jobs 2`), but answered within it in this run (`--jobs 1`). This is the same defect, near the 600 s limit.

**Root cause: the Phase 5 SQL change, the year pushdown** (`9769f52`, `_YEAR_PUSHDOWN` in `src/atus_pipeline/analytics/queries.py`). The evidence comes from EXPLAIN (no ANALYZE) and read-only counts:

1. The planner estimates 12 respondents for the population filter. The true counts are 5,868 (`population-071`) and 6,666 (`measures-weights-043`). The filter's predicates are correlated: `data_year`, `pandemic_weight > 0` and the `diary_date` window select almost the same rows. The planner multiplies their selectivities as if they were independent, and no extended statistics exist on `atus.*`. The serving database estimates 13, so the misestimate is the same there.
2. Without the pushdown (the Phase 2-4 SQL that target a runs), `activity_minutes` aggregates every year's episodes, and the planner estimates its output at about 115,000 rows. It then picks a Hash Right Join, and the aggregate runs once.
3. With the pushdown, the aggregate is estimated at 13,000-18,000 rows and looks cheap. Given the 1-row outer estimate, the planner picks a **Nested Loop Left Join whose inner side is the activity aggregate**: Finalize GroupAggregate → Gather Merge → Partial GroupAggregate → Parallel Bitmap Heap Scan on `activities`. A nested loop rescans its inner side once per outer row, and a Gather Merge rescan reruns the whole parallel aggregate. With 5,868 real outer rows, the activity aggregate is recomputed about 5,868 times.
4. On the serving database, the same Phase 5 SQL plans the inner aggregate as a non-parallel HashAggregate. For `population-071` it plans a Hash Right Join. For `measures-weights-043` it plans a nested loop, but PostgreSQL reuses an unparameterized hash aggregate's table on rescan, so the aggregate runs once. Both answer.

What it is not:

- **Not the serving database build.** c answered, and a vs c is equivalent on all 544 specs.
- **Not an API change.** The API sends the engine's SQL unchanged.
- **Not the criterion.** The status difference is real: b gave no answer.

**How widespread (EXPLAIN-only survey of the 418 specs that reach the database).** With the Phase 5 SQL on the full database, 32 specs get the hazard plan: a nested-loop left join that reruns a non-hashed activity aggregate for every outer row, with an estimated 1 outer row. With the Phase 2-4 SQL, 3 specs get it (`measures-weights-060`, `measures-weights-065`, `population-076`; true populations 991, 58 and 22). On the serving database, 0 specs get it with either SQL. The pushdown did not create the misestimate, but it makes the bad plan about ten times as frequent and puts it on large populations.

The respondent counts below come from the served answers. All of these specs answered within 600 s in this run except `population-071`. Whether they fit the 85 s production budget is for the later timed run.

| Spec | Request | True respondents (outer rows) |
|---|---|---:|
| `measures-weights-043` | TV, participants/day, pandemic 2020, 05-10..12-31 | 6,666 |
| `population-071` | eating and drinking, participants/day, pandemic 2019, 05-10..12-31 | 5,868 (timed out) |
| `measures-weights-059` | pandemic 2020, 03-01..06-30 | 2,171 |
| `population-074` | participants/day, pandemic 2020, 03-01..06-30 (non-parallel inner) | 2,171 |
| `measures-weights-042` | minutes/participant, pandemic 2020, 01-01..03-17 | 2,116 |
| `errors-weird-024` | participants/day, pandemic 2019 weekends, 03-01..06-30 | 1,018 |
| `measures-weights-057` | participants/day, pandemic 2020, California, from 05-10 (non-parallel inner) | 614 |

The other 25 hazard specs have 0-39 respondents, or are refused with `insufficient_data`.

**Fix options for the engine-SQL owner (WS-B).** None was applied here: statistics code is not changed in this run.

- `WITH activity_minutes AS MATERIALIZED (...)`: on the full database, EXPLAIN then shows the aggregate computed once and a Hash Right Join, for both specs. This is EXPLAIN only and was not executed.
- Revert to the Phase 2-4 join shape.
- Extended statistics on `respondents (data_year, diary_date, pandemic_weight)`. These would need a schema change on both databases, and their effect on range predicates is unproven.

Any fix needs this run repeated (`scripts/equivalence/run.sh`, or `--failed-from` plus the 32 hazard specs). It also needs the later timed run on the hazard specs above.
