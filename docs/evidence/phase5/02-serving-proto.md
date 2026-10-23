# Prototype serving database (Phase 5 audit, 2026-10-20)

This prototype serving-database build was produced during the Phase 5 audit. It tests the §2 design: whether the full 1,980 MiB database can be reduced to a set that fits the free managed Postgres tiers without changing any served number. It is **not repository code**. It is the starting point for the §9 builder (`atus build-serving-db` or `scripts/build_serving_db.py`), which still has to be written with tests.

**Provenance**

- Built by the audit agent `exp-serving-proto` from commit 387fc2d.
- Build id: `proto-20261020T172359Z-e5748e84`.
- Target: container `atus-emu-pg`, image `postgres:16` (PostgreSQL 16.15), host port 5441, volume `atus-emu-pgdata`. The container was left running for the implementation phase.
- Source: the full database `atus` on port 5434. It was read in one `REPEATABLE READ READ ONLY` snapshot and never written by the builder.

**Raw outputs** are in `docs/evidence/phase5/audit/exp-serving-proto/`, called `$P` below. This is session scratch space and is not committed. The three source files are reproduced verbatim at the end of this document.

**Timings are illustrative.** Other audit agents were using the same machine and Postgres server while this prototype was built and measured. Sizes, row counts, hashes and equivalence results do not depend on load.

## Design

The engine and API code are unchanged. The serving DB keeps every table name, retained column name and type, NULL semantics, and CHECK constraint the engine depends on. The only exceptions are listed in the table.

| Table | Full DB | Serving prototype | Why results cannot change |
|---|---|---|---|
| `activities` | 4,994,172 episode rows | 2,982,182 rows, one per (tucaseid, activity_code), `duration_minutes` = summed minutes (`smallint`). Rows are inserted `ORDER BY activity_code, tucaseid`. | The engine reads activities only as `sum(duration_minutes) ... WHERE activity_code = ANY(...) GROUP BY tucaseid` (`src/atus_pipeline/analytics/queries.py:123-128`). `duration_minutes` is NOT NULL, so integer sums are exact. The summed column is kept `smallint` so `sum()` still returns `bigint`, not `numeric`. |
| `activity_companions` | 6,358,042 rows | dropped | The engine, API and `validate-analytics` never read it. |
| `replicate_weights` | 258,954 rows, `numeric(18,6)` | 250,172 rows, `float8` | The engine already casts each replicate to `float8` (`queries.py:79-81`). 0 of 40,027,520 cells change in a `numeric→float8→numeric` round trip. The 8,782 dropped rows are all NULL and belong exactly to the 2020 respondents with `final_weight IS NULL`, so `final_weight > 0` already excludes them. |
| `pandemic_replicate_weights` | 18,217 rows | 16,751 rows, `float8` | 0 of 2,914,720 cells change in the round trip. The 1,466 dropped rows are all zero and belong exactly to respondents with `pandemic_weight = 0`. |
| `household_members`, `cps_persons` | all lines | line 1 only, needed columns only. `lineno` is kept, and a `CHECK (lineno = 1)` is added. | The engine joins `ON ... lineno = 1` with inner-join semantics (`queries.py:99-104`). |
| `respondents` | all columns | the 8 columns the engine and API read | Point weights stay `numeric(18,6)`, so full-sample sums stay exact. All 258,954 rows are kept, because `/meta` counts them. |
| lexicon, `ingestion_runs`, `validation_results`, `source_files` | | lexicon copied. One `succeeded` `ingestion_runs` row is written with the build id and content hash in `notes`. `validation_results` and `source_files` are kept empty. | `atus validate-analytics` INSERTs into `validation_results` (`src/atus_pipeline/analytics/benchmarks.py:304-318`). |

The build process runs in this order:

1. Apply the repo migrations.
2. Recreate the data tables in serving shape (`serving_schema.sql`). Recreating them, rather than dropping columns, means no dropped-attribute slots remain in the tuples.
3. Assert the source invariants. The build stops if any assertion count is non-zero.
4. Stream each table with binary `COPY` from the source snapshot.
5. Add keys, indexes and FKs (`serving_post_load.sql`). The index names match `migrations/0002`.
6. Compute a content hash twice: once on the target, and once independently from the source transformation queries. The two must match.
7. Write the `ingestion_runs` row.
8. Run `VACUUM (ANALYZE)`.

## Build log (`$P/build_log.txt`, `$P/build_report.json`)

| Step / table | Rows | Binary COPY bytes | Seconds |
|---|---:|---:|---:|
| migrations + serving schema | | | 0.04 |
| source assertions (6, all 0) | | | 9.16 |
| `activity_tier1` / `activity_tier2` / `activity_codes` | 18 / 107 / 431 | 695 / 5,966 / 32,065 | 0.00 |
| `respondents` | 258,954 | 18,260,665 | 0.91 |
| `household_members` | 258,954 | 8,286,549 | 0.87 |
| `cps_persons` | 258,954 | 9,840,273 | 1.64 |
| `activities` | 2,982,182 | 89,465,481 | 13.36 |
| `replicate_weights` | 250,172 | 483,832,669 | 5.35 |
| `pandemic_replicate_weights` | 16,751 | 32,396,455 | 0.23 |
| keys, indexes, FKs | | | 1.37 |
| target content hash | | | 5.56 |
| source-derived content hash | | | 9.34 |
| `VACUUM (ANALYZE)` | | | 1.28 |
| **total** | | | **49.22** |

## Footprint

Source files: full DB `$P/full_db_footprint.txt`; prototype immediately after the build `$P/proto_footprint.txt`; final state after `validation_results` was truncated and a second `VACUUM (ANALYZE)` ran `$P/proto_footprint_final.txt`. Sizes are in MiB (1 MiB = 2^20 bytes). The "MB" figures in brief §2 are MiB.

| Table | Full DB rows | Full DB total MiB | Serving rows | Serving total MiB (final) |
|---|---:|---:|---:|---:|
| `activities` | 4,994,172 | 724.8 | 2,982,182 | 168.3 (heap 148.4, index 19.8) |
| `activity_companions` | 6,358,042 | 547.4 | | |
| `replicate_weights` | 258,954 | 495.1 | 250,172 | 331.2 |
| `cps_persons` | 681,624 | 81.6 | 258,954 | 22.5 |
| `household_members` | 702,409 | 50.9 | 258,954 | 18.8 |
| `respondents` | 258,954 | 35.7 | 258,954 | 26.1 |
| `pandemic_replicate_weights` | 18,217 | 34.3 | 16,751 | 22.2 |
| lexicon + metadata tables | | < 0.5 | | ≈ 0.4 |
| **user objects** | | **1,970.1** | | **589.5** (591.3 right after the build) |
| `pg_database_size('atus')` | | 2,076,277,783 B = 1,980.1 MiB | | 627,457,047 B = 598.4 MiB (628,997,143 B right after the build) |
| `sum(pg_database_size(datname))` over the cluster | | n/a (the 5434 cluster holds other databases) | | 650,458,708 B = 620.3 MiB (651,998,804 B right after the build) |

The Neon-metered size is about 0.65 GB. That leaves about 350 MB (decimal) of headroom under the 1 GB Free limit before any WAL or history growth during a restore. Growth during a restore was not measured.

An optional unique index on `activities (tucaseid, activity_code)` would add 94,068,736 bytes (90 MiB) (`$P/activities_pk_option.txt`). The prototype omits it. Uniqueness holds by `GROUP BY` construction and is covered by the content-hash match.

## §2 claims checked on the full DB (`$P/verify_claims_full.txt`, read-only session)

- **activities:** 4,994,172 episodes in 2,982,182 (tucaseid, code) groups. Summed minutes per group range from 1 to 1436, with 0 groups summing to 0. There are 0 zero-minute episodes. Every respondent's total is exactly 1440, and every respondent has at least one activity row.
- **replicate_weights:**
  - 8,782 all-NULL rows; all are `data_year = 2020` with `final_weight IS NULL`. 0 rows are partly NULL.
  - 0 respondents with `final_weight > 0` lack a row.
  - 40,027,520 non-NULL cells; 0 differ after the `float8` round trip.
- **pandemic_replicate_weights:**
  - 1,466 all-zero rows; all have `pandemic_weight = 0`. 0 rows have a NULL cell.
  - 0 respondents with `pandemic_weight = 0` have non-zero replicates.
  - 2,914,720 cells; 0 differ after the round trip.
- **Line-1 rows:** both `household_members` and `cps_persons` have 258,954 line-1 rows, and no respondent lacks one. Line-1 rows have 0 NULLs in age, sex, region, state_fips and education.
- **Fixture caveat:** the hand-computed fixture does contain a NULL education value and a 0-minute episode. The fixture serving build (below) reproduced both.

## Content hash

The hash is SHA-256 over `COPY (SELECT ... ORDER BY key) TO STDOUT (FORMAT text)` for every served table, combined per table with its column list and row count. It excludes `ingestion_runs`, `validation_results` and `source_files`. Output-affecting settings are pinned: `extra_float_digits = 1` (shortest exact `float8` text), `DateStyle ISO, YMD`, `TimeZone UTC` and `client_encoding UTF8`.

- **Target hash** = **source-derived hash** = `e8fa76a7c5b8f6e21d0d7f3309118f0b6aed230fe999e546e4c3cdffda293b23`. All 9 per-table hashes match (`$P/build_report.json`), so every `float8` cell is bit-identical to `numeric::float8`.
- The hash was unchanged at the end of the audit (`$P/final_state.txt`).
- **Not done:** two builds from the full DB were never compared. The fixture build reproduced its own source-derived hash (`6bd5c532…ea59`, `$P/fixture_check.txt`). §9 still requires a test that builds twice from the fixture DB and compares the hashes.

## Equivalence (full DB vs prototype)

| Check | Result | Evidence |
|---|---|---|
| `atus validate-analytics` | 24/24 pass on both DBs. The printed benchmark output is identical; the only differing line is the timing line, which contains the URL. | `$P/validate_analytics_full.txt`, `$P/validate_analytics_proto.txt` |
| Engine diff at full precision: 24 benchmarks, 12 analyses (6 estimates, 3 trends, 3 compares, all with replicate variance, covering roster/CPS joins, pandemic weights, the 2020 window, the topcode warning and pooled 22-year runs), and 3 error cases | All 1,727 exact-compared fields are equal: point estimates, `n_respondents`, `n_participants`, weighted population, warnings and error messages. Largest relative SE difference 1.28e-13 (2015 point of the sleep trend); largest relative CI difference 1.74e-15. In the SE benchmark the two values are 0.029303478519167595 and 0.029303478519169278 (3.2e-14 relative). | `$P/compare_engine.txt` |
| Same-DB repeat noise (3–4 identical runs) | Full DB vs itself: up to 8.57e-14 relative SE difference. Serving vs itself: up to 7.48e-14. Point values identical across runs. | `$P/self_noise_full.txt`, `$P/self_noise_serving.txt` |
| Near-universal participation | Sleep participation pooled over 22 years: 6.24e-13 relative SE difference. Personal-care participation pooled over 22 years: 3.60e-13. Largest observed \|ΔSE\| / \|estimate\| is 7.34e-17. | `$P/extremes.txt`, `$P/extremes2.txt` |
| True-zero-variance spec (all 18 tier-1 codes, average minutes, 2025; estimate 1440) | SE 7.83e-12 on the full DB vs 7.93e-12 on serving, a relative difference of 1.31e-2. Both are floating-point noise; point value and CI are identical. **A purely relative 1e-12 SE tolerance fails here**, so the §9 criterion needs a scale floor. A refuter reproduced the same noise on the full DB alone by toggling parallel workers. | `$P/extremes.txt` |
| `ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_analytics_benchmarks.py tests/data_quality/test_api_benchmarks.py` against the prototype | 11 passed. Repo `git status` was unchanged before and after the run. | `$P/pytest_dq_serving.txt`, `$P/git_status_*_pytest.txt` |
| API (TestClient) | GET endpoints and the trend and compare requests (variance `none`) are byte-identical. `/meta` differs only in `last_ingested_at`; both report `ingestion_run_id: 1`. The estimate with variance differs only in SE noise. | `$P/api_get_diff.txt` |
| Fixture: full fixture DB vs a fixture serving build through the same builder | 281 specs give byte-identical JSON, error responses included. This covers the 0-minute episode (E3), all-NULL 2020 replicates and the all-zero pandemic row. | `$P/fixture_check.txt` |

## Query plans and JIT (illustrative timings)

The worst-case spec was sleep, pooled over 2003–2025 excluding 2020, with 160 replicates, rendered exactly by `queries.build_query` (`$P/explain_worst_*.txt`, `$P/jit_probe_*.txt`, `$P/jit_heavy_*.txt`).

| | Full DB | Serving prototype |
|---|---|---|
| Activity access | Bitmap Heap Scan, 567,822 rows × 3 loops, 185,900 buffers | Index Scan on `idx_activities_activity_code`, 271,705 rows × 3 loops, 5,912–5,915 buffers |
| Estimated cost | 678,161 | 442,008 |
| `EXPLAIN ANALYZE` execution, `jit=on` (default) | 11,530 / 12,492 ms (JIT with inlining and optimization; JIT total 16.6–18.3 s summed over 3 processes) | 1,491 / 1,474 ms (expression JIT only; JIT total 745–888 ms) |
| Execution with `jit=off` | 5,289 / 5,266 ms | 949 / 881 ms |
| Broad selector: all 18 tier-1 codes pooled 22 years, `jit=on` / `off` | 15,677 / 10,927 ms | 3,904 / 2,440 ms (cost 1,099,033 crosses the inlining threshold) |
| BLS leisure pooled 22 years, `jit=on` / `off` | 11,890 / 5,390 ms | 1,620 / 935 ms |

JIT settings were the same on both servers: `jit_above_cost = 100000`, `jit_inline_above_cost = jit_optimize_above_cost = 500000`. The buffer contrast in the first row is due partly to pre-aggregation and narrower rows, not only to physical ordering.

## Dump and restore (`$P/dump_restore.txt`)

- `pg_dump -Fc` (client 16.15) produced 336,547,206 bytes in 23 s, sha256 `e2f45a24…7a79`. The dump's sha256 is not expected to repeat across builds; the content hash is.
- `pg_restore --no-owner --no-acl` into a fresh database took 6 s. The restored database is 628,867,599 bytes. After `VACUUM`, `pg_stats.correlation` for `activities.activity_code` is still 1, so the physical order survives dump and restore. The scratch restore database was then dropped.
- The schema comment marking the database as a serving build survives the restore.

## Problems found with this design

1. **`data_version` collision.** The serving DB's `ingestion_runs` identity starts at 1, so `read_data_version()` (`src/atus_pipeline/api/dependencies.py:50-60`) returns `0325:run1` on both the prototype and the full DB (`$P/final_state.txt`). The real builder must write a globally unique version: release + build id + content hash.
2. **`public.schema_migrations` is misleading.** It records 0001 and 0002 as applied although the tables are reshaped. `atus migrate` would report the database as up to date. `atus load` fails on the missing `activity_companions` and rolls back, but leaves a `failed` `ingestion_runs` row behind. `atus validate-db` fails with `UndefinedTable`. Neither the schema comment nor the `notes` marker is checked by the CLI. The builder needs an explicit serving marker that `load`, `migrate` and `validate-db` refuse to run against.
3. **Equivalence tolerance needs a scale floor.** See the zero-variance row above.
4. **JIT.** With the default `jit=on`, about 55% of the full DB's worst case is JIT compile time. Neon computes are built without LLVM (Neon's public compute Dockerfile has no `--with-llvm`; not verified on a live project). Emulations and the API role should use `jit=off`.
5. **Writes from `validate-analytics`.** Running it against the prototype inserted 24 `validation_results` rows; they were truncated afterwards (`$P/final_state.txt`). Run it only as the owner role, never as the API role.
6. **Expected failures.** `atus validate-db` and `tests/data_quality/test_full_database.py` fail on the serving DB by design (row counts, companions, diary arithmetic, staged files). Run them on the full DB before each build.

---

## Source: `build_serving_proto.py` (verbatim)

```python
"""PROTOTYPE serving-database builder (Phase 5 audit; seeds `atus build-serving-db`).

    python build_serving_proto.py --source <full_db_url> --target <empty_db_url> \
        --repo /path/to/ATUS [--build-id ID]

What it does
  1. apply_migrations(target, repo/migrations)  -> canonical schema + schema_migrations
  2. serving_schema.sql (one txn)               -> recreate data tables in serving shape
  3. stream every served table source -> target with binary COPY, all source
     reads inside ONE `REPEATABLE READ READ ONLY` transaction (consistent
     snapshot; the source is never written)
  4. build-time equivalence assertions on the source snapshot (dropped replicate
     rows are invisible to the engine's `weight > 0` filter, aggregated minutes fit)
  5. serving_post_load.sql                      -> PKs, indexes (same names as 0002), FKs
  6. content hash over every served table (fixed order, excluding the build row),
     computed on the TARGET and, independently, from the SOURCE snapshot's
     transformation queries -> must be identical (cell-exact incl. float8 bits)
  7. one `succeeded` ingestion_runs row carrying build id + content hash in notes
  8. VACUUM (ANALYZE)

The source is only ever read. Nothing is written outside the target database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

HERE = Path(__file__).resolve().parent
R = [f"tufnwgtp{i:03d}" for i in range(1, 161)]
P = [f"tu20fwgt{i:03d}" for i in range(1, 161)]


def _cols(cols, cast=""):
    return ", ".join(f"{c}{cast}" for c in cols)


# (target table, target column list, source SELECT, ORDER BY key used for hashing)
TABLES = [
    ("activity_tier1", "code, name",
     "SELECT code, name FROM atus.activity_tier1 ORDER BY code", "code"),
    ("activity_tier2", "code, tier1_code, name",
     "SELECT code, tier1_code, name FROM atus.activity_tier2 ORDER BY code", "code"),
    ("activity_codes", "code, tier2_code, name, harmonization_note",
     "SELECT code, tier2_code, name, harmonization_note FROM atus.activity_codes ORDER BY code",
     "code"),
    ("respondents",
     "tucaseid, data_year, diary_date, diary_day_of_week, labor_force_status, "
     "household_children, final_weight, pandemic_weight",
     "SELECT tucaseid, data_year, diary_date, diary_day_of_week, labor_force_status, "
     "household_children, final_weight, pandemic_weight FROM atus.respondents ORDER BY tucaseid",
     "tucaseid"),
    ("household_members", "tucaseid, lineno, age, sex",
     "SELECT tucaseid, lineno, age, sex FROM atus.household_members WHERE lineno = 1 "
     "ORDER BY tucaseid", "tucaseid, lineno"),
    ("cps_persons", "tucaseid, lineno, region, state_fips, education",
     "SELECT tucaseid, lineno, region, state_fips, education FROM atus.cps_persons "
     "WHERE lineno = 1 ORDER BY tucaseid", "tucaseid, lineno"),
    ("activities", "tucaseid, activity_code, duration_minutes",
     "SELECT tucaseid, activity_code, sum(duration_minutes)::smallint FROM atus.activities "
     "GROUP BY tucaseid, activity_code ORDER BY activity_code, tucaseid",
     "activity_code, tucaseid"),
    ("replicate_weights", "tucaseid, " + _cols(R),
     f"SELECT tucaseid, {_cols(R, '::float8')} FROM atus.replicate_weights "
     f"WHERE NOT ({' AND '.join(f'{c} IS NULL' for c in R)}) ORDER BY tucaseid",
     "tucaseid"),
    ("pandemic_replicate_weights", "tucaseid, " + _cols(P),
     f"SELECT tucaseid, {_cols(P, '::float8')} FROM atus.pandemic_replicate_weights "
     f"WHERE NOT ({' AND '.join(f'{c} = 0' for c in P)}) ORDER BY tucaseid",
     "tucaseid"),
]

# Build-time assertions on the SOURCE snapshot: each must return 0.
ASSERTIONS = {
    "dropped all-NULL replicate rows whose respondent has final_weight > 0":
        "SELECT count(*) FROM atus.replicate_weights w JOIN atus.respondents r USING (tucaseid) "
        f"WHERE ({' AND '.join(f'w.{c} IS NULL' for c in R)}) AND r.final_weight > 0",
    "kept replicate rows with any NULL cell (partial NULLs would change sums)":
        f"SELECT count(*) FROM atus.replicate_weights WHERE ({' OR '.join(f'{c} IS NULL' for c in R)}) "
        f"AND NOT ({' AND '.join(f'{c} IS NULL' for c in R)})",
    "dropped all-zero pandemic rows whose respondent has pandemic_weight > 0":
        "SELECT count(*) FROM atus.pandemic_replicate_weights w JOIN atus.respondents r USING (tucaseid) "
        f"WHERE ({' AND '.join(f'w.{c} = 0' for c in P)}) AND r.pandemic_weight > 0",
    "per-(tucaseid, code) summed minutes outside 0..1440":
        "SELECT count(*) FROM (SELECT sum(duration_minutes) m FROM atus.activities "
        "GROUP BY tucaseid, activity_code) g WHERE m NOT BETWEEN 0 AND 1440",
    "replicate cells whose numeric -> float8 -> numeric round trip differs":
        "SELECT coalesce(sum(" + " + ".join(f"({c}::float8::numeric <> {c})::int" for c in R)
        + "), 0) FROM atus.replicate_weights",
    "pandemic replicate cells whose numeric -> float8 -> numeric round trip differs":
        "SELECT coalesce(sum(" + " + ".join(f"({c}::float8::numeric <> {c})::int" for c in P)
        + "), 0) FROM atus.pandemic_replicate_weights",
}


def _same_server_db(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.hostname, pa.port, pa.path) == (pb.hostname, pb.port, pb.path)


def _hash_query(conn: psycopg.Connection, sql: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with conn.cursor() as cur:
        with cur.copy(f"COPY ({sql}) TO STDOUT (FORMAT text)") as copy:
            for chunk in copy:
                h.update(chunk)
                n += bytes(chunk).count(b"\n")
    return h.hexdigest(), n


def _set_text_output(conn: psycopg.Connection) -> None:
    # Pin every GUC that affects COPY text output so the hash is reproducible.
    conn.execute("SET extra_float_digits = 1")   # shortest exact float8 text
    conn.execute("SET DateStyle = 'ISO, YMD'")
    conn.execute("SET TimeZone = 'UTC'")
    conn.execute("SET client_encoding = 'UTF8'")


def content_hash(conn: psycopg.Connection, side: str) -> dict:
    """side='target': hash the served tables; side='source': hash the
    transformation queries over the full DB (must match the target)."""
    _set_text_output(conn)
    per_table = {}
    combined = hashlib.sha256()
    for name, cols, source_sql, key in TABLES:
        sql = source_sql if side == "source" else f"SELECT {cols} FROM atus.{name} ORDER BY {key}"
        digest, rows = _hash_query(conn, sql)
        per_table[name] = {"sha256": digest, "rows": rows}
        combined.update(f"{name}\t{cols}\t{rows}\t{digest}\n".encode())
    return {"content_hash": combined.hexdigest(), "tables": per_table}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--build-id", default=None)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()
    if _same_server_db(args.source, args.target):
        sys.exit("refusing: source and target are the same database")

    sys.path.insert(0, str(args.repo / "src"))
    from atus_pipeline.database.migrate import apply_migrations  # noqa: E402

    build_id = args.build_id or f"proto-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    report: dict = {"build_id": build_id, "steps": {}}
    t_all = time.perf_counter()

    def step(name, t0):
        report["steps"][name] = round(time.perf_counter() - t0, 2)
        print(f"[{report['steps'][name]:8.2f}s] {name}", flush=True)

    with psycopg.connect(args.target) as tgt:
        exists = tgt.execute("SELECT to_regnamespace('atus') IS NOT NULL").fetchone()[0]
        if exists:
            sys.exit("refusing: target already has schema atus (build into an empty database)")
        t0 = time.perf_counter()
        applied = apply_migrations(tgt, args.repo / "migrations")
        report["migrations_applied"] = applied
        with tgt.transaction():
            tgt.execute((HERE / "serving_schema.sql").read_text())
        step("migrations + serving schema", t0)

    # One consistent, read-only snapshot of the full database.
    src = psycopg.connect(args.source, options="-c default_transaction_read_only=on")
    src.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    assert src.execute("SHOW transaction_read_only").fetchone()[0] == "on"
    run = src.execute(
        "SELECT id, release, pipeline_version, finished_at FROM atus.ingestion_runs "
        "WHERE status = 'succeeded' ORDER BY id DESC LIMIT 1").fetchone()
    report["source_run"] = {"id": run[0], "release": run[1], "pipeline_version": run[2],
                            "finished_at": run[3].isoformat()}

    t0 = time.perf_counter()
    failures = {}
    for label, sql in ASSERTIONS.items():
        n = src.execute(sql).fetchone()[0]
        report.setdefault("assertions", {})[label] = int(n)
        if n:
            failures[label] = int(n)
    step("source assertions", t0)
    if failures:
        sys.exit(f"equivalence assertions failed: {failures}")

    with psycopg.connect(args.target) as tgt:
        report["copy"] = {}
        for name, cols, source_sql, _ in TABLES:
            t0 = time.perf_counter()
            nbytes = 0
            with src.cursor() as scur, tgt.cursor() as tcur:
                with scur.copy(f"COPY ({source_sql}) TO STDOUT (FORMAT binary)") as out, \
                        tcur.copy(f"COPY atus.{name} ({cols}) FROM STDIN (FORMAT binary)") as inp:
                    for chunk in out:
                        nbytes += len(chunk)
                        inp.write(chunk)
            tgt.commit()
            rows = tgt.execute(f"SELECT count(*) FROM atus.{name}").fetchone()[0]
            report["copy"][name] = {"rows": rows, "binary_bytes": nbytes,
                                    "seconds": round(time.perf_counter() - t0, 2)}
            print(f"  copied {name}: {rows:,} rows, {nbytes/2**20:.1f} MiB binary", flush=True)
        t0 = time.perf_counter()
        with tgt.transaction():
            tgt.execute((HERE / "serving_post_load.sql").read_text())
        step("keys, indexes, FKs", t0)

        t0 = time.perf_counter()
        target_hash = content_hash(tgt, "target")
        step("target content hash", t0)

    t0 = time.perf_counter()
    source_hash = content_hash(src, "source")
    step("source-derived content hash", t0)
    src.execute("COMMIT")
    src.close()

    report["content_hash_target"] = target_hash
    report["content_hash_source"] = source_hash
    report["content_hash_match"] = target_hash["content_hash"] == source_hash["content_hash"]
    mismatched = [t for t in target_hash["tables"]
                  if target_hash["tables"][t] != source_hash["tables"][t]]
    report["content_hash_mismatched_tables"] = mismatched

    with psycopg.connect(args.target) as tgt:
        with tgt.transaction():
            tgt.execute(
                "INSERT INTO atus.ingestion_runs (release, pipeline_version, finished_at, status, notes) "
                "VALUES (%s, %s, now(), 'succeeded', %s)",
                (run[1], run[2], json.dumps({
                    "kind": "serving-prototype", "build_id": build_id,
                    "content_hash": target_hash["content_hash"],
                    "source_run_id": run[0]})))
            tgt.execute(
                "COMMENT ON SCHEMA atus IS %s" if False else
                f"COMMENT ON SCHEMA atus IS 'ATUS SERVING database (prototype) build {build_id}; "
                f"derived read-only from the full DB; do not run atus load/migrate here'")
        report["ingestion_run"] = tgt.execute(
            "SELECT id, release, status FROM atus.ingestion_runs").fetchall()

    with psycopg.connect(args.target, autocommit=True) as tgt:
        t0 = time.perf_counter()
        tgt.execute("VACUUM (ANALYZE)")
        step("VACUUM (ANALYZE)", t0)

    report["total_seconds"] = round(time.perf_counter() - t_all, 2)
    print(json.dumps({k: report[k] for k in ("build_id", "content_hash_match",
                                             "content_hash_mismatched_tables",
                                             "assertions", "total_seconds")}, indent=2))
    print("target content hash:", target_hash["content_hash"])
    print("source content hash:", source_hash["content_hash"])
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, default=str))
    if not report["content_hash_match"]:
        sys.exit("CONTENT HASH MISMATCH")


if __name__ == "__main__":
    main()

```

## Source: `serving_schema.sql` (verbatim)

```sql
-- serving_schema.sql  (PROTOTYPE, Phase 5 audit)
-- Applied AFTER migrations/0001+0002 have created the canonical schema, in one
-- transaction. Recreates (rather than ALTER ... DROP COLUMN) the data tables so
-- no dropped-attribute slots remain in the tuples (a dropped column is stored
-- as NULL and forces a null bitmap on every row).
-- Table names, retained column names/types (except replicate weights -> float8
-- and activities -> one row per (tucaseid, activity_code)), NULL semantics and
-- the CHECK constraints on retained columns are copied verbatim from 0001.
-- Keys, indexes and FKs are added by serving_post_load.sql after COPY.

DROP TABLE atus.activity_companions;          -- never read by engine/API/validate-analytics; its FK targets the dropped episode PK
DROP TABLE atus.activities;
DROP TABLE atus.replicate_weights;
DROP TABLE atus.pandemic_replicate_weights;
DROP TABLE atus.cps_persons;
DROP TABLE atus.household_members;
DROP TABLE atus.respondents;

-- 8 columns the engine/API read (queries.py, population.py, engine.py, meta.py, health.py)
CREATE TABLE atus.respondents (
    tucaseid                        bigint NOT NULL,
    data_year                       smallint NOT NULL CHECK (data_year >= 2003),
    diary_date                      date NOT NULL,
    diary_day_of_week               smallint NOT NULL CHECK (diary_day_of_week BETWEEN 1 AND 7),
    labor_force_status              smallint NOT NULL CHECK (labor_force_status BETWEEN 1 AND 5),
    household_children              smallint CHECK (household_children BETWEEN 0 AND 30),
    final_weight                    numeric(18,6) CHECK (final_weight >= 0),
    pandemic_weight                 numeric(18,6) CHECK (pandemic_weight >= 0),
    CONSTRAINT final_weight_2020_rule CHECK ((data_year = 2020) = (final_weight IS NULL)),
    CONSTRAINT pandemic_weight_years CHECK (pandemic_weight IS NULL OR data_year IN (2019, 2020))
);

-- respondent's own roster row only (engine joins ON hm.lineno = 1)
CREATE TABLE atus.household_members (
    tucaseid     bigint NOT NULL,
    lineno       smallint NOT NULL CHECK (lineno >= 1),
    age          smallint CHECK (age BETWEEN 0 AND 85),
    sex          smallint NOT NULL CHECK (sex IN (1, 2)),
    CONSTRAINT serving_line1_only CHECK (lineno = 1)
);

-- respondent's own CPS row only (engine joins ON c.lineno = 1)
CREATE TABLE atus.cps_persons (
    tucaseid               bigint NOT NULL,
    lineno                 smallint NOT NULL CHECK (lineno >= 1),
    region                 smallint CHECK (region BETWEEN 1 AND 4),
    state_fips             char(2) CHECK (state_fips ~ '^[0-9]{2}$'),
    education              smallint CHECK (education BETWEEN 31 AND 46),
    CONSTRAINT serving_line1_only CHECK (lineno = 1)
);

-- one row per (tucaseid, activity_code); duration_minutes = SUM of the episode
-- minutes for that code (max observed 1436, so the 0-1440 CHECK still holds).
-- Rows are inserted ORDER BY activity_code, tucaseid: that physical order is
-- what makes the activity_code index scan cheap.
CREATE TABLE atus.activities (
    tucaseid                    bigint NOT NULL,
    activity_code               char(6) NOT NULL,
    duration_minutes            smallint NOT NULL CHECK (duration_minutes BETWEEN 0 AND 1440)
);

-- float8 = exactly numeric(18,6)::float8, the cast queries.py applies anyway.
-- All-NULL 2020 rows dropped (respondents.final_weight IS NULL for all of them).
CREATE TABLE atus.replicate_weights (
    tucaseid            bigint NOT NULL,
    tufnwgtp001       float8,
    tufnwgtp002       float8,
    tufnwgtp003       float8,
    tufnwgtp004       float8,
    tufnwgtp005       float8,
    tufnwgtp006       float8,
    tufnwgtp007       float8,
    tufnwgtp008       float8,
    tufnwgtp009       float8,
    tufnwgtp010       float8,
    tufnwgtp011       float8,
    tufnwgtp012       float8,
    tufnwgtp013       float8,
    tufnwgtp014       float8,
    tufnwgtp015       float8,
    tufnwgtp016       float8,
    tufnwgtp017       float8,
    tufnwgtp018       float8,
    tufnwgtp019       float8,
    tufnwgtp020       float8,
    tufnwgtp021       float8,
    tufnwgtp022       float8,
    tufnwgtp023       float8,
    tufnwgtp024       float8,
    tufnwgtp025       float8,
    tufnwgtp026       float8,
    tufnwgtp027       float8,
    tufnwgtp028       float8,
    tufnwgtp029       float8,
    tufnwgtp030       float8,
    tufnwgtp031       float8,
    tufnwgtp032       float8,
    tufnwgtp033       float8,
    tufnwgtp034       float8,
    tufnwgtp035       float8,
    tufnwgtp036       float8,
    tufnwgtp037       float8,
    tufnwgtp038       float8,
    tufnwgtp039       float8,
    tufnwgtp040       float8,
    tufnwgtp041       float8,
    tufnwgtp042       float8,
    tufnwgtp043       float8,
    tufnwgtp044       float8,
    tufnwgtp045       float8,
    tufnwgtp046       float8,
    tufnwgtp047       float8,
    tufnwgtp048       float8,
    tufnwgtp049       float8,
    tufnwgtp050       float8,
    tufnwgtp051       float8,
    tufnwgtp052       float8,
    tufnwgtp053       float8,
    tufnwgtp054       float8,
    tufnwgtp055       float8,
    tufnwgtp056       float8,
    tufnwgtp057       float8,
    tufnwgtp058       float8,
    tufnwgtp059       float8,
    tufnwgtp060       float8,
    tufnwgtp061       float8,
    tufnwgtp062       float8,
    tufnwgtp063       float8,
    tufnwgtp064       float8,
    tufnwgtp065       float8,
    tufnwgtp066       float8,
    tufnwgtp067       float8,
    tufnwgtp068       float8,
    tufnwgtp069       float8,
    tufnwgtp070       float8,
    tufnwgtp071       float8,
    tufnwgtp072       float8,
    tufnwgtp073       float8,
    tufnwgtp074       float8,
    tufnwgtp075       float8,
    tufnwgtp076       float8,
    tufnwgtp077       float8,
    tufnwgtp078       float8,
    tufnwgtp079       float8,
    tufnwgtp080       float8,
    tufnwgtp081       float8,
    tufnwgtp082       float8,
    tufnwgtp083       float8,
    tufnwgtp084       float8,
    tufnwgtp085       float8,
    tufnwgtp086       float8,
    tufnwgtp087       float8,
    tufnwgtp088       float8,
    tufnwgtp089       float8,
    tufnwgtp090       float8,
    tufnwgtp091       float8,
    tufnwgtp092       float8,
    tufnwgtp093       float8,
    tufnwgtp094       float8,
    tufnwgtp095       float8,
    tufnwgtp096       float8,
    tufnwgtp097       float8,
    tufnwgtp098       float8,
    tufnwgtp099       float8,
    tufnwgtp100       float8,
    tufnwgtp101       float8,
    tufnwgtp102       float8,
    tufnwgtp103       float8,
    tufnwgtp104       float8,
    tufnwgtp105       float8,
    tufnwgtp106       float8,
    tufnwgtp107       float8,
    tufnwgtp108       float8,
    tufnwgtp109       float8,
    tufnwgtp110       float8,
    tufnwgtp111       float8,
    tufnwgtp112       float8,
    tufnwgtp113       float8,
    tufnwgtp114       float8,
    tufnwgtp115       float8,
    tufnwgtp116       float8,
    tufnwgtp117       float8,
    tufnwgtp118       float8,
    tufnwgtp119       float8,
    tufnwgtp120       float8,
    tufnwgtp121       float8,
    tufnwgtp122       float8,
    tufnwgtp123       float8,
    tufnwgtp124       float8,
    tufnwgtp125       float8,
    tufnwgtp126       float8,
    tufnwgtp127       float8,
    tufnwgtp128       float8,
    tufnwgtp129       float8,
    tufnwgtp130       float8,
    tufnwgtp131       float8,
    tufnwgtp132       float8,
    tufnwgtp133       float8,
    tufnwgtp134       float8,
    tufnwgtp135       float8,
    tufnwgtp136       float8,
    tufnwgtp137       float8,
    tufnwgtp138       float8,
    tufnwgtp139       float8,
    tufnwgtp140       float8,
    tufnwgtp141       float8,
    tufnwgtp142       float8,
    tufnwgtp143       float8,
    tufnwgtp144       float8,
    tufnwgtp145       float8,
    tufnwgtp146       float8,
    tufnwgtp147       float8,
    tufnwgtp148       float8,
    tufnwgtp149       float8,
    tufnwgtp150       float8,
    tufnwgtp151       float8,
    tufnwgtp152       float8,
    tufnwgtp153       float8,
    tufnwgtp154       float8,
    tufnwgtp155       float8,
    tufnwgtp156       float8,
    tufnwgtp157       float8,
    tufnwgtp158       float8,
    tufnwgtp159       float8,
    tufnwgtp160       float8
);

-- All-zero rows dropped (respondents.pandemic_weight = 0 for all of them).
CREATE TABLE atus.pandemic_replicate_weights (
    tucaseid            bigint NOT NULL,
    tu20fwgt001       float8,
    tu20fwgt002       float8,
    tu20fwgt003       float8,
    tu20fwgt004       float8,
    tu20fwgt005       float8,
    tu20fwgt006       float8,
    tu20fwgt007       float8,
    tu20fwgt008       float8,
    tu20fwgt009       float8,
    tu20fwgt010       float8,
    tu20fwgt011       float8,
    tu20fwgt012       float8,
    tu20fwgt013       float8,
    tu20fwgt014       float8,
    tu20fwgt015       float8,
    tu20fwgt016       float8,
    tu20fwgt017       float8,
    tu20fwgt018       float8,
    tu20fwgt019       float8,
    tu20fwgt020       float8,
    tu20fwgt021       float8,
    tu20fwgt022       float8,
    tu20fwgt023       float8,
    tu20fwgt024       float8,
    tu20fwgt025       float8,
    tu20fwgt026       float8,
    tu20fwgt027       float8,
    tu20fwgt028       float8,
    tu20fwgt029       float8,
    tu20fwgt030       float8,
    tu20fwgt031       float8,
    tu20fwgt032       float8,
    tu20fwgt033       float8,
    tu20fwgt034       float8,
    tu20fwgt035       float8,
    tu20fwgt036       float8,
    tu20fwgt037       float8,
    tu20fwgt038       float8,
    tu20fwgt039       float8,
    tu20fwgt040       float8,
    tu20fwgt041       float8,
    tu20fwgt042       float8,
    tu20fwgt043       float8,
    tu20fwgt044       float8,
    tu20fwgt045       float8,
    tu20fwgt046       float8,
    tu20fwgt047       float8,
    tu20fwgt048       float8,
    tu20fwgt049       float8,
    tu20fwgt050       float8,
    tu20fwgt051       float8,
    tu20fwgt052       float8,
    tu20fwgt053       float8,
    tu20fwgt054       float8,
    tu20fwgt055       float8,
    tu20fwgt056       float8,
    tu20fwgt057       float8,
    tu20fwgt058       float8,
    tu20fwgt059       float8,
    tu20fwgt060       float8,
    tu20fwgt061       float8,
    tu20fwgt062       float8,
    tu20fwgt063       float8,
    tu20fwgt064       float8,
    tu20fwgt065       float8,
    tu20fwgt066       float8,
    tu20fwgt067       float8,
    tu20fwgt068       float8,
    tu20fwgt069       float8,
    tu20fwgt070       float8,
    tu20fwgt071       float8,
    tu20fwgt072       float8,
    tu20fwgt073       float8,
    tu20fwgt074       float8,
    tu20fwgt075       float8,
    tu20fwgt076       float8,
    tu20fwgt077       float8,
    tu20fwgt078       float8,
    tu20fwgt079       float8,
    tu20fwgt080       float8,
    tu20fwgt081       float8,
    tu20fwgt082       float8,
    tu20fwgt083       float8,
    tu20fwgt084       float8,
    tu20fwgt085       float8,
    tu20fwgt086       float8,
    tu20fwgt087       float8,
    tu20fwgt088       float8,
    tu20fwgt089       float8,
    tu20fwgt090       float8,
    tu20fwgt091       float8,
    tu20fwgt092       float8,
    tu20fwgt093       float8,
    tu20fwgt094       float8,
    tu20fwgt095       float8,
    tu20fwgt096       float8,
    tu20fwgt097       float8,
    tu20fwgt098       float8,
    tu20fwgt099       float8,
    tu20fwgt100       float8,
    tu20fwgt101       float8,
    tu20fwgt102       float8,
    tu20fwgt103       float8,
    tu20fwgt104       float8,
    tu20fwgt105       float8,
    tu20fwgt106       float8,
    tu20fwgt107       float8,
    tu20fwgt108       float8,
    tu20fwgt109       float8,
    tu20fwgt110       float8,
    tu20fwgt111       float8,
    tu20fwgt112       float8,
    tu20fwgt113       float8,
    tu20fwgt114       float8,
    tu20fwgt115       float8,
    tu20fwgt116       float8,
    tu20fwgt117       float8,
    tu20fwgt118       float8,
    tu20fwgt119       float8,
    tu20fwgt120       float8,
    tu20fwgt121       float8,
    tu20fwgt122       float8,
    tu20fwgt123       float8,
    tu20fwgt124       float8,
    tu20fwgt125       float8,
    tu20fwgt126       float8,
    tu20fwgt127       float8,
    tu20fwgt128       float8,
    tu20fwgt129       float8,
    tu20fwgt130       float8,
    tu20fwgt131       float8,
    tu20fwgt132       float8,
    tu20fwgt133       float8,
    tu20fwgt134       float8,
    tu20fwgt135       float8,
    tu20fwgt136       float8,
    tu20fwgt137       float8,
    tu20fwgt138       float8,
    tu20fwgt139       float8,
    tu20fwgt140       float8,
    tu20fwgt141       float8,
    tu20fwgt142       float8,
    tu20fwgt143       float8,
    tu20fwgt144       float8,
    tu20fwgt145       float8,
    tu20fwgt146       float8,
    tu20fwgt147       float8,
    tu20fwgt148       float8,
    tu20fwgt149       float8,
    tu20fwgt150       float8,
    tu20fwgt151       float8,
    tu20fwgt152       float8,
    tu20fwgt153       float8,
    tu20fwgt154       float8,
    tu20fwgt155       float8,
    tu20fwgt156       float8,
    tu20fwgt157       float8,
    tu20fwgt158       float8,
    tu20fwgt159       float8,
    tu20fwgt160       float8
);


```

## Source: `serving_post_load.sql` (verbatim)

```sql
-- serving_post_load.sql (PROTOTYPE): keys, indexes and FKs added after COPY so
-- they are built in one pass (compact) and FKs are validated once.
ALTER TABLE atus.respondents ADD PRIMARY KEY (tucaseid);
ALTER TABLE atus.household_members ADD PRIMARY KEY (tucaseid, lineno);
ALTER TABLE atus.cps_persons ADD PRIMARY KEY (tucaseid, lineno);
ALTER TABLE atus.replicate_weights ADD PRIMARY KEY (tucaseid);
ALTER TABLE atus.pandemic_replicate_weights ADD PRIMARY KEY (tucaseid);

-- same names as migrations/0002 for the retained columns
CREATE INDEX idx_respondents_data_year ON atus.respondents (data_year);
CREATE INDEX idx_respondents_diary_date ON atus.respondents (diary_date);
CREATE INDEX idx_activities_activity_code ON atus.activities (activity_code);
CREATE INDEX idx_cps_persons_state_fips ON atus.cps_persons (state_fips);

ALTER TABLE atus.household_members ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
ALTER TABLE atus.cps_persons ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
ALTER TABLE atus.activities ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
ALTER TABLE atus.activities ADD FOREIGN KEY (activity_code) REFERENCES atus.activity_codes (code);
ALTER TABLE atus.replicate_weights ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
ALTER TABLE atus.pandemic_replicate_weights ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
```
