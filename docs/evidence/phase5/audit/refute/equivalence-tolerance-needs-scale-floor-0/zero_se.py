import psycopg
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure
url="postgresql://atus:atus@localhost:5434/atus"
with psycopg.connect(url, options="-c default_transaction_read_only=on") as c:
    t1=[r[0] for r in c.execute("select code from atus.activity_tier1 order by 1")]
    e=AnalysisEngine(c)
    for par in ("2","0"):
        c.execute(f"set max_parallel_workers_per_gather={par}")
        for i in range(3):
            r=e.estimate(AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, ActivitySelector(tuple(t1),label="all"),(2025,))).estimate
            print("workers",par,"run",i,"value",repr(r.value),"SE",repr(r.standard_error))
