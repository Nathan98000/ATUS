import sys, subprocess, tempfile, traceback, logging
from pathlib import Path
REPO = Path("$REPO"); sys.path.insert(0, str(REPO))
import psycopg
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from atus_pipeline.validation.db_checks import run_db_checks
from tests.integration.analytics_dataset import analytics_settings
logging.basicConfig(level=logging.INFO)
P = Path("docs/evidence/phase5/audit/exp-serving-proto")
HERE = Path(__file__).resolve().parent
ADMIN = "postgresql://atus:atus@localhost:5434/postgres"
FULL, SRV = "atus_audit_refute_ssm_full", "atus_audit_refute_ssm_srv"
url = lambda d: f"postgresql://atus:atus@localhost:5434/{d}"
def snap(c):
    out = {}
    for t in ("respondents","activities","replicate_weights","household_members","cps_persons","activity_codes","ingestion_runs"):
        try: out[t] = c.execute(f"select count(*) from atus.{t}").fetchone()[0]
        except Exception as e: c.rollback(); out[t] = f"ERR {e}"
    return out
with psycopg.connect(ADMIN, autocommit=True) as a:
    for d in (FULL, SRV): a.execute(f'DROP DATABASE IF EXISTS "{d}"'); a.execute(f'CREATE DATABASE "{d}"')
try:
    with tempfile.TemporaryDirectory(dir=HERE) as tmp:
        settings = analytics_settings(Path(tmp), url(FULL))
        with psycopg.connect(url(FULL)) as c: apply_migrations(c, REPO/"migrations"); load_all(c, settings)
        r = subprocess.run([sys.executable, str(P/"build_serving_proto.py"), "--source", url(FULL), "--target", url(SRV),
                            "--repo", str(REPO), "--report", str(HERE/"build_report.json")], capture_output=True, text=True)
        print("builder exit", r.returncode, r.stdout[-800:], r.stderr[-800:])
        settings_srv = analytics_settings(Path(tmp)/"s", url(SRV))
        with psycopg.connect(url(SRV)) as c:
            print("schema_migrations:", c.execute("select version from public.schema_migrations order by 1").fetchall())
            print("BEFORE:", snap(c))
            print("== migrate"); print("applied:", apply_migrations(c, REPO/"migrations"))
        with psycopg.connect(url(SRV)) as c:
            print("== load")
            try: load_all(c, settings_srv); print("load SUCCEEDED")
            except Exception as e: print("load FAILED:", type(e).__name__, str(e)[:300])
        with psycopg.connect(url(SRV)) as c:
            print("AFTER load:", snap(c))
            print("ingestion_runs:", c.execute("select id,status,left(notes,200) from atus.ingestion_runs order by id").fetchall())
        with psycopg.connect(url(SRV)) as c:
            print("== validate-db")
            try:
                res = run_db_checks(c, settings_srv)
                for x in res: print("  ", x)
            except Exception as e: print("validate-db RAISED:", type(e).__name__, str(e)[:300])
finally:
    with psycopg.connect(ADMIN, autocommit=True) as a:
        for d in (FULL, SRV): a.execute(f'DROP DATABASE IF EXISTS "{d}"')
    print("dropped")
