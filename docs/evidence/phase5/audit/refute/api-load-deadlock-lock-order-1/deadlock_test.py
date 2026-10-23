"""E5 (scratch DB only): lock-order interaction between one API analysis transaction
(real engine code, real query order) and the loader's TRUNCATE (real table list).
The TRUNCATE transaction is always rolled back."""
import threading, time
import psycopg
from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.analytics.spec import AnalysisSpec, ActivitySelector, Measure, PopulationFilter
from atus_pipeline.api.dependencies import read_data_version
from atus_pipeline.loading.loader import _CANONICAL_TABLES
URL = "postgresql://atus:atus@localhost:5434/atus_refute_dl"
spec = AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY, activity=ActivitySelector(include=("010101",)),
                    years=(2023,), population=PopulationFilter(), weights="multiyear", variance="replicate",
                    confidence_level=0.95)
a = psycopg.connect(URL)            # API-like: non-autocommit, one transaction per request
b = psycopg.connect(URL)            # loader-like
print("deadlock_timeout:", a.execute("SHOW deadlock_timeout").fetchone()[0]); a.rollback()
t0 = time.monotonic()
dv = read_data_version(a)
eng = AnalysisEngine(a)
prepared = eng._prepare(spec)        # reads respondents + lexicon -> AccessShare locks held to txn end
print(f"t={time.monotonic()-t0:.2f} API txn holds locks after data_version={dv} + _prepare")
res = {}
def loader():
    try:
        b.execute("TRUNCATE " + ", ".join(f"atus.{t}" for t in _CANONICAL_TABLES))
        res["loader"] = "TRUNCATE acquired all locks"
    except Exception as e:
        res["loader"] = f"{type(e).__module__}.{type(e).__name__}: {e}".splitlines()[0]
    finally:
        b.rollback()   # never commit: scratch DB stays loaded
th = threading.Thread(target=loader); th.start()
time.sleep(0.3)
print(f"t={time.monotonic()-t0:.2f} loader TRUNCATE started; API now runs the sufficient-statistics query")
try:
    stats = eng._fetch(spec, prepared)
    res["api"] = f"OK {list(stats)}"
except Exception as e:
    res["api"] = f"{type(e).__module__}.{type(e).__name__} (isinstance OperationalError={isinstance(e, psycopg.OperationalError)}): {e}".splitlines()[0]
print(f"t={time.monotonic()-t0:.2f} API query returned: {res['api']}")
a.rollback()
th.join()
print(f"t={time.monotonic()-t0:.2f} loader: {res['loader']}")
