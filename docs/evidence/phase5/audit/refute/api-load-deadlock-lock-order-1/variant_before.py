"""Variant: loader TRUNCATE begins BEFORE the API request touches any table -> API just waits (no deadlock)."""
import threading, time, psycopg
from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.analytics.spec import AnalysisSpec, ActivitySelector, Measure, PopulationFilter
from atus_pipeline.loading.loader import _CANONICAL_TABLES
URL="postgresql://atus:atus@localhost:5434/atus_refute_dl"
spec = AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY, activity=ActivitySelector(include=("010101",)),
  years=(2023,), population=PopulationFilter(), weights="multiyear", variance="replicate", confidence_level=0.95)
a=psycopg.connect(URL); b=psycopg.connect(URL); t0=time.monotonic()
b.execute("TRUNCATE " + ", ".join(f"atus.{t}" for t in _CANONICAL_TABLES)); print("loader holds all locks")
def rel(): time.sleep(2); b.rollback(); print(f"t={time.monotonic()-t0:.2f} loader rolled back")
threading.Thread(target=rel).start()
try:
    r=AnalysisEngine(a).estimate(spec); print(f"t={time.monotonic()-t0:.2f} API OK")
except Exception as e: print(f"t={time.monotonic()-t0:.2f} API {type(e).__name__}: {e}".splitlines()[0])
a.rollback()
