import psycopg
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure
url="postgresql://atus:atus@localhost:5434/atus"
P22 = tuple(y for y in range(2003, 2026) if y != 2020)
with psycopg.connect(url, options="-c default_transaction_read_only=on") as c:
    t1=tuple(r[0] for r in c.execute("select code from atus.activity_tier1 order by 1"))
    print(t1)
    # check every respondent sums to 1440 in 2025
    print(c.execute("select min(s),max(s),count(*) from (select a.tucaseid,sum(a.duration_minutes) s from atus.activity a join atus.respondent r using(tucaseid) where r.year=2025 group by 1) q").fetchall() if False else "")
    e=AnalysisEngine(c); ALL=ActivitySelector(t1,label="all")
    for m,yrs in [(Measure.AVERAGE_MINUTES_PER_DAY,(2025,)),(Measure.AVERAGE_MINUTES_PER_PARTICIPANT,P22)]:
        r=e.estimate(AnalysisSpec(m,ALL,yrs)).estimate
        print(m, repr(r.value), repr(r.standard_error), r.ci_lower, r.ci_upper)
