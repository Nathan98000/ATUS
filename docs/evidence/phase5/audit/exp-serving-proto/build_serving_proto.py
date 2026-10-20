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
