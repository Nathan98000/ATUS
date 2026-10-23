import sys, tempfile, psycopg
from pathlib import Path
R=Path("$REPO"); sys.path.insert(0,str(R))
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from atus_pipeline.api.dependencies import read_data_version
from tests.integration.analytics_dataset import analytics_settings
admin="postgresql://atus:atus@localhost:5434/postgres"
for name in ["atus_refute_dv_a","atus_refute_dv_b"]:
    with psycopg.connect(admin,autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}"'); c.execute(f'CREATE DATABASE "{name}"')
    url=f"postgresql://atus:atus@localhost:5434/{name}"
    with psycopg.connect(url) as c: apply_migrations(c, R/"migrations")
    with tempfile.TemporaryDirectory() as t:
        s=analytics_settings(Path(t),url)
        with psycopg.connect(url) as c:
            load_all(c,s); print(name,"after first load:",read_data_version(c))
            if name.endswith("a"):
                load_all(c,s); print(name,"after reload same db:",read_data_version(c))
with psycopg.connect("postgresql://atus:atus@localhost:5434/atus") as c:
    print("full db atus:", read_data_version(c))
with psycopg.connect(admin,autocommit=True) as c:
    for name in ["atus_refute_dv_a","atus_refute_dv_b"]: c.execute(f'DROP DATABASE "{name}"')
print("dropped")
