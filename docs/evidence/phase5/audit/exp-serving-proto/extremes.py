"""Degenerate/small-SE specs: full vs serving SE differences.
usage: python extremes.py <full_url> <serving_url>"""
import sys, psycopg
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure
full, serv = sys.argv[1], sys.argv[2]
ALL = ActivitySelector(tuple(f"{c:02d}" for c in (1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,18,50)), label="all")
sleep = ActivitySelector.preset("sleep")
P22 = tuple(y for y in range(2003, 2026) if y != 2020)
specs = {
 "sleep participation 2025": AnalysisSpec(Measure.PARTICIPATION_RATE, sleep, (2025,)),
 "sleep participation pooled 22y": AnalysisSpec(Measure.PARTICIPATION_RATE, sleep, P22),
 "ALL codes avg minutes 2025 (true SE = 0)": AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, ALL, (2025,)),
 "ALL codes participation 2025 (true SE = 0)": AnalysisSpec(Measure.PARTICIPATION_RATE, ALL, (2025,)),
 "ALL codes min/participant pooled 22y (true SE = 0)": AnalysisSpec(Measure.AVERAGE_MINUTES_PER_PARTICIPANT, ALL, P22),
}
opts = "-c default_transaction_read_only=on"
with psycopg.connect(full, options=opts) as cf, psycopg.connect(serv, options=opts) as cs:
    tier1 = [r[0] for r in cf.execute("select code from atus.activity_tier1 order by 1")]
    print("tier1 codes:", tier1)
    ef, es = AnalysisEngine(cf), AnalysisEngine(cs)
    for name, spec in specs.items():
        if "ALL" in name:
            spec = AnalysisSpec(spec.measure, ActivitySelector(tuple(tier1), label="all"), spec.years)
        a = ef.estimate(spec).estimate; b = es.estimate(spec).estimate
        rel = abs(a.standard_error - b.standard_error) / abs(a.standard_error) if a.standard_error else float("nan")
        print(f"{name:52s} value full={a.value!r} serv={b.value!r} same={a.value == b.value} | "
              f"SE full={a.standard_error!r} serv={b.standard_error!r} relSEdiff={rel:.2e} | "
              f"CI full=({a.ci_lower!r},{a.ci_upper!r}) serv=({b.ci_lower!r},{b.ci_upper!r})", flush=True)
