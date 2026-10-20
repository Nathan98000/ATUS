"""Full-vs-serving diff through the real AnalysisEngine (read-only sessions).

    python compare_engine.py <full_url> <serving_url> <out.json>

Runs (a) all 24 benchmark specs (values extracted at full float precision,
WITHOUT run_benchmarks' INSERT into validation_results) and (b) 10+
representative estimate/trend/compare analyses with replicate variance, plus
error cases, on both databases, and diffs the complete result dicts:
  - every field except SE / CI bounds must be exactly equal;
  - standard_error, ci_lower, ci_upper: max relative difference is reported.
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import date

import psycopg

from atus_pipeline.analytics import (
    ActivitySelector, AnalysisEngine, AnalysisSpec, ComparisonSpec, Measure, PopulationFilter,
)
from atus_pipeline.analytics.benchmarks import build_benchmarks

FULL, SERV, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
TOL_FIELDS = {"standard_error", "ci_lower", "ci_upper"}
POOLED22 = tuple(y for y in range(2003, 2026) if y != 2020)

sleep = ActivitySelector.preset("sleep")
leisure = ActivitySelector.preset("leisure_and_sports_bls_table")
house = ActivitySelector.preset("household_activities_bls_table")
work = ActivitySelector.preset("work_related_bls_table")
tv = ActivitySelector.preset("watching_tv")

CASES = [
    ("E1 estimate sleep 2025", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2025,))),
    ("E2 estimate leisure women 25-54 employed 2025 (roster join)", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, leisure, (2025,),
                  PopulationFilter(sex="female", age_min=25, age_max=54, employment_status="employed"))),
    ("E3 estimate sleep pooled 22 years (worst case)", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, POOLED22)),
    ("E4 estimate participants/day work, South, weekday, 2010 (CPS join)", "estimate",
     AnalysisSpec(Measure.PARTICIPANTS_PER_DAY, work, (2010,),
                  PopulationFilter(region=3, day_type="weekday"))),
    ("E5 estimate min/participant housework, CA, children, pooled 2015-2019", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_PARTICIPANT, ActivitySelector.preset("housework"),
                  (2015, 2016, 2017, 2018, 2019),
                  PopulationFilter(state_fips="06", has_household_children=True))),
    ("E6 estimate TV 2020 pandemic weights May10-Dec31", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, tv, (2020,),
                  PopulationFilter(diary_date_min=date(2020, 5, 10), diary_date_max=date(2020, 12, 31)),
                  weights="pandemic")),
    ("T1 trend sleep 2003-2025 multiyear (2020 unavailable)", "trend",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, tuple(range(2003, 2026)))),
    ("T2 trend TV 2019-2020 pandemic weights, participation", "trend",
     AnalysisSpec(Measure.PARTICIPATION_RATE, tv, (2019, 2020), weights="pandemic")),
    ("T3 trend leisure 2003-2019 men 65+ (topcode warning)", "trend",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, leisure, tuple(range(2003, 2020)),
                  PopulationFilter(sex="male", age_min=65, age_max=85))),
    ("C1 compare household activities 2025 men vs women", "compare",
     ComparisonSpec(AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, house, (2025,)),
                    PopulationFilter(sex="male"), PopulationFilter(sex="female"), "men", "women")),
    ("C2 compare work participation bachelor+ vs <HS pooled 22y", "compare",
     ComparisonSpec(AnalysisSpec(Measure.PARTICIPATION_RATE, work, POOLED22),
                    PopulationFilter(education_level="bachelor_or_higher"),
                    PopulationFilter(education_level="less_than_high_school"))),
    ("C3 compare 2020 pandemic employed vs NILF, eating, excl. window", "compare",
     ComparisonSpec(AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, ActivitySelector.preset("eating_and_drinking"),
                                 (2020,), PopulationFilter(diary_date_min=date(2020, 5, 10)), weights="pandemic"),
                    PopulationFilter(employment_status="employed"),
                    PopulationFilter(employment_status="not_in_labor_force"))),
    ("X1 error: multiyear weights with 2020", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2019, 2020))),
    ("X2 error: unknown activity code", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, ActivitySelector(("999999",)), (2025,))),
    ("X3 error: year not loaded", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2026,))),
]


def run(engine, mode, spec):
    t = time.perf_counter()
    try:
        res = getattr(engine, mode)(spec)
        out = {"ok": res.to_dict()}
    except Exception as exc:  # error responses must be identical too
        out = {"error": f"{type(exc).__name__}: {exc}"}
    return out, time.perf_counter() - t


def walk(a, b, path, stats):
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            stats["exact_mismatch"].append((path, "keys", sorted(set(a) ^ set(b))))
        for k in a.keys() & b.keys():
            walk(a[k], b[k], f"{path}.{k}", stats)
    elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            stats["exact_mismatch"].append((path, "len", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            walk(x, y, f"{path}[{i}]", stats)
    else:
        leaf = path.rsplit(".", 1)[-1]
        if leaf in TOL_FIELDS and isinstance(a, float) and isinstance(b, float):
            stats["tol_fields"] += 1
            if a == b:
                stats["tol_exact"] += 1
            rel = abs(a - b) / abs(a) if a else (0.0 if b == 0 else math.inf)
            if leaf == "standard_error":
                stats["max_rel_se"] = max(stats["max_rel_se"], rel)
            else:
                stats["max_rel_ci"] = max(stats["max_rel_ci"], rel)
        else:
            stats["exact_fields"] += 1
            if a != b:
                stats["exact_mismatch"].append((path, a, b))


def main():
    opts = "-c default_transaction_read_only=on"
    with psycopg.connect(FULL, options=opts) as cf, psycopg.connect(SERV, options=opts) as cs:
        ef, es = AnalysisEngine(cf), AnalysisEngine(cs)
        report = {"benchmarks": [], "cases": []}
        overall = {"max_rel_se": 0.0, "max_rel_ci": 0.0, "exact_mismatch": [], "exact_fields": 0,
                   "tol_fields": 0, "tol_exact": 0}
        for bm in build_benchmarks():
            rf, tf = run(ef, "estimate", bm.spec)
            rs, ts = run(es, "estimate", bm.spec)
            vf = bm.extract(ef.estimate(bm.spec)) if "ok" in rf else None
            vs = bm.extract(es.estimate(bm.spec)) if "ok" in rs else None
            stats = {"max_rel_se": 0.0, "max_rel_ci": 0.0, "exact_mismatch": [], "exact_fields": 0,
                     "tol_fields": 0, "tol_exact": 0}
            walk(rf, rs, "result", stats)
            report["benchmarks"].append({
                "name": bm.name, "full_value": vf, "serving_value": vs,
                "value_identical": vf == vs, "expected": bm.expected,
                "max_rel_se": stats["max_rel_se"], "exact_mismatch": stats["exact_mismatch"],
                "seconds_full": round(tf, 3), "seconds_serving": round(ts, 3)})
            for k in ("max_rel_se", "max_rel_ci"):
                overall[k] = max(overall[k], stats[k])
            for k in ("exact_fields", "tol_fields", "tol_exact"):
                overall[k] += stats[k]
            overall["exact_mismatch"] += stats["exact_mismatch"]
            print(f"BM {bm.name[:70]:70s} full={vf!r} serv={vs!r} same={vf == vs} "
                  f"relSE={stats['max_rel_se']:.2e} mism={len(stats['exact_mismatch'])}", flush=True)
        for name, mode, spec in CASES:
            rf, tf = run(ef, mode, spec)
            rs, ts = run(es, mode, spec)
            stats = {"max_rel_se": 0.0, "max_rel_ci": 0.0, "exact_mismatch": [], "exact_fields": 0,
                     "tol_fields": 0, "tol_exact": 0}
            walk(rf, rs, "result", stats)
            for k in ("max_rel_se", "max_rel_ci"):
                overall[k] = max(overall[k], stats[k])
            for k in ("exact_fields", "tol_fields", "tol_exact"):
                overall[k] += stats[k]
            overall["exact_mismatch"] += [(name, *m) for m in stats["exact_mismatch"]]
            kind = "error" if "error" in rf else "ok"
            report["cases"].append({"name": name, "mode": mode, "kind": kind,
                                    "error_full": rf.get("error"), "error_serving": rs.get("error"),
                                    "max_rel_se": stats["max_rel_se"], "max_rel_ci": stats["max_rel_ci"],
                                    "exact_fields": stats["exact_fields"], "tol_fields": stats["tol_fields"],
                                    "tol_exact": stats["tol_exact"],
                                    "exact_mismatch": stats["exact_mismatch"],
                                    "seconds_full": round(tf, 3), "seconds_serving": round(ts, 3),
                                    "full": rf, "serving": rs})
            print(f"CASE {name[:62]:62s} {kind:5s} relSE={stats['max_rel_se']:.2e} "
                  f"relCI={stats['max_rel_ci']:.2e} exact={stats['exact_fields']} "
                  f"mism={len(stats['exact_mismatch'])} tol_exact={stats['tol_exact']}/{stats['tol_fields']} "
                  f"t_full={tf:.2f}s t_serv={ts:.2f}s", flush=True)
        report["overall"] = overall
        print("OVERALL", json.dumps({k: v for k, v in overall.items()}, default=str))
        with open(OUT, "w") as fh:
            json.dump(report, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
