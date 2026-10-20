import sys, psycopg
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure
url=sys.argv[1]
with psycopg.connect(url, options="-c default_transaction_read_only=on") as c:
    t1=[r[0] for r in c.execute("select code from atus.activity_tier1 order by 1")]
    e=AnalysisEngine(c)
    spec=AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, ActivitySelector(tuple(t1),label="all"),(2025,))
    ses=[]
    for i in range(5):
        r=e.estimate(spec).estimate; ses.append(r.standard_error)
        print(i, repr(r.value), repr(r.standard_error), repr(r.ci_lower), repr(r.ci_upper), flush=True)
    b=ses[0]; print("max rel SE diff vs run0:", max(abs(s-b)/b for s in ses) if b else "nan")
