import psycopg
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure
with psycopg.connect("postgresql://atus:atus@localhost:5434/atus", options="-c default_transaction_read_only=on") as c:
    t1=[r[0] for r in c.execute("select code from atus.activity_tier1 order by 1")]
    e=AnalysisEngine(c)
    for m in (Measure.AVERAGE_MINUTES_PER_DAY, Measure.PARTICIPATION_RATE):
        r=e.estimate(AnalysisSpec(m, ActivitySelector(tuple(t1),label="all"),(2025,))).estimate
        print(m, repr(r.value), repr(r.standard_error), r.ci_lower, r.ci_upper)
