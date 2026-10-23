"""Builder smoke on the hand-computed fixture (0-minute episode, 10-episode
grain case, all-NULL 2020 replicates, all-zero pandemic row).
Creates scratch DBs atus_audit_protofix_full / atus_audit_protofix_srv on the
5434 server, loads the fixture through the REAL loader into *_full, builds the
serving prototype into *_srv, diffs engine output, then drops both DBs."""
import itertools, json, os, subprocess, sys, tempfile
from datetime import date
from pathlib import Path
import psycopg
REPO = Path("$REPO")
sys.path.insert(0, str(REPO))
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, ComparisonSpec, Measure, PopulationFilter
from tests.integration.analytics_dataset import analytics_settings

ADMIN = "postgresql://atus:atus@localhost:5434/postgres"
FULL_DB, SRV_DB = "atus_audit_protofix_full", "atus_audit_protofix_srv"
assert FULL_DB.startswith("atus_audit_") and SRV_DB.startswith("atus_audit_")
url = lambda db: f"postgresql://atus:atus@localhost:5434/{db}"
HERE = Path(__file__).resolve().parent

with psycopg.connect(ADMIN, autocommit=True) as a:
    for db in (FULL_DB, SRV_DB):
        a.execute(f'DROP DATABASE IF EXISTS "{db}"'); a.execute(f'CREATE DATABASE "{db}"')
try:
    with psycopg.connect(url(FULL_DB)) as conn:
        apply_migrations(conn, REPO / "migrations")
    with tempfile.TemporaryDirectory(dir=HERE) as tmp:
        settings = analytics_settings(Path(tmp), url(FULL_DB))
        with psycopg.connect(url(FULL_DB)) as conn:
            load_all(conn, settings)
    r = subprocess.run([sys.executable, str(HERE / "build_serving_proto.py"), "--source", url(FULL_DB),
                        "--target", url(SRV_DB), "--repo", str(REPO),
                        "--report", str(HERE / "fixture_build_report.json")], capture_output=True, text=True)
    print(r.stdout[-2500:], r.stderr[-2500:], "builder exit", r.returncode)
    with psycopg.connect(url(SRV_DB)) as c:
        print("serving fixture rows:", {t: c.execute(f"select count(*) from atus.{t}").fetchone()[0] for t in
              ("respondents", "activities", "replicate_weights", "pandemic_replicate_weights", "household_members", "cps_persons")})
        print("serving activities rows:", c.execute("select tucaseid, activity_code, duration_minutes from atus.activities order by activity_code, tucaseid").fetchall())
    SLEEP, TV = ActivitySelector(("010101",)), ActivitySelector(("120303",))
    specs = []
    for m, act, pop in itertools.product(list(Measure), (SLEEP, TV), (
            PopulationFilter(), PopulationFilter(sex="female"), PopulationFilter(education_level="bachelor_or_higher"),
            PopulationFilter(region=3), PopulationFilter(age_min=60), PopulationFilter(has_household_children=True),
            PopulationFilter(day_type="weekday"))):
        specs.append(("estimate", AnalysisSpec(m, act, (2023,), pop)))
        specs.append(("estimate", AnalysisSpec(m, act, (2019, 2023), pop)))
        specs.append(("trend", AnalysisSpec(m, act, (2019, 2020, 2023), pop)))
        specs.append(("estimate", AnalysisSpec(m, act, (2019, 2020), pop, weights="pandemic")))
        specs.append(("compare", ComparisonSpec(AnalysisSpec(m, act, (2023,)), PopulationFilter(sex="male"), PopulationFilter(sex="female"))))
    specs.append(("estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, SLEEP, (2019,),
                  PopulationFilter(diary_date_min=date(2019, 5, 10)), weights="pandemic")))
    same = diff = 0; bad = []
    with psycopg.connect(url(FULL_DB)) as cf, psycopg.connect(url(SRV_DB)) as cs:
        ef, es = AnalysisEngine(cf), AnalysisEngine(cs)
        for mode, spec in specs:
            out = []
            for e in (ef, es):
                try: out.append(json.dumps(getattr(e, mode)(spec).to_dict(), sort_keys=True, default=str))
                except Exception as exc: out.append(f"ERR {type(exc).__name__}: {exc}")
            if out[0] == out[1]: same += 1
            else: diff += 1; bad.append((mode, str(spec)[:120], out[0][:300], out[1][:300]))
    print(f"fixture specs: {len(specs)}; byte-identical JSON results (incl. errors): {same}; different: {diff}")
    for b in bad[:10]: print("DIFF", b)
finally:
    with psycopg.connect(ADMIN, autocommit=True) as a:
        for db in (FULL_DB, SRV_DB):
            a.execute(f'DROP DATABASE IF EXISTS "{db}"')
    print("dropped scratch DBs")
