"""Near-universal participation (SE << value): full vs serving relative SE diff.
usage: python extremes2.py <full_url> <serving_url>"""
import sys, psycopg
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure, PopulationFilter
full, serv = sys.argv[1], sys.argv[2]
P22 = tuple(y for y in range(2003, 2026) if y != 2020)
S = lambda *c: ActivitySelector(tuple(c))
specs = {
 "01 personal care participation pooled 22y": AnalysisSpec(Measure.PARTICIPATION_RATE, S("01"), P22),
 "01 personal care avg minutes pooled 22y": AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, S("01"), P22),
 "11 eating participation pooled 22y": AnalysisSpec(Measure.PARTICIPATION_RATE, S("11"), P22),
 "0101 sleep participation pooled 22y women": AnalysisSpec(Measure.PARTICIPATION_RATE, S("0101"), P22, PopulationFilter(sex="female")),
 "0101 sleep min/participant pooled 22y": AnalysisSpec(Measure.AVERAGE_MINUTES_PER_PARTICIPANT, S("0101"), P22),
 "01 personal care participation 2003-2019 (17y)": AnalysisSpec(Measure.PARTICIPATION_RATE, S("01"), tuple(range(2003, 2020))),
 "0101 sleep persons/day pooled 22y": AnalysisSpec(Measure.PARTICIPANTS_PER_DAY, S("0101"), P22),
}
opts = "-c default_transaction_read_only=on"
with psycopg.connect(full, options=opts) as cf, psycopg.connect(serv, options=opts) as cs:
    ef, es = AnalysisEngine(cf), AnalysisEngine(cs)
    for name, spec in specs.items():
        a = ef.estimate(spec).estimate; b = es.estimate(spec).estimate
        d = abs(a.standard_error - b.standard_error)
        rel = d / abs(a.standard_error) if a.standard_error else float("nan")
        print(f"{name:46s} value_same={a.value == b.value} value={a.value:.10g} SE={a.standard_error:.6g} "
              f"SE/value={a.standard_error/a.value:.2e} relSEdiff={rel:.2e} |dSE|/value={d/abs(a.value):.2e}", flush=True)
