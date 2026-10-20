import psycopg
from pathlib import Path
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.api.dependencies import read_data_version
admin="postgresql://atus:atus@localhost:5434/postgres"
with psycopg.connect(admin, autocommit=True) as c:
    c.execute("DROP DATABASE IF EXISTS atus_audit_dvrefute"); c.execute("CREATE DATABASE atus_audit_dvrefute")
try:
    with psycopg.connect("postgresql://atus:atus@localhost:5434/atus_audit_dvrefute", autocommit=True) as c:
        print("applied", apply_migrations(c, Path("migrations")))
        rid=c.execute("INSERT INTO atus.ingestion_runs (release, pipeline_version) VALUES ('0325','x') RETURNING id").fetchone()[0]
        c.execute("UPDATE atus.ingestion_runs SET status='succeeded', finished_at=now() WHERE id=%s",(rid,))
        print("scratch (empty data) data_version:", read_data_version(c))
        print("scratch respondents:", c.execute("select count(*) from atus.respondents").fetchone()[0])
    with psycopg.connect("postgresql://atus:atus@localhost:5434/atus") as c:
        print("full DB data_version:", read_data_version(c))
        print("full respondents:", c.execute("select count(*) from atus.respondents").fetchone()[0])
finally:
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS atus_audit_dvrefute")
