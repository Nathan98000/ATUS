"""Leave the prototype in its intended state and re-verify it:
truncate validation_results (validate-analytics/pytest inserted rows), VACUUM
(ANALYZE), recompute the content hash, show both data_versions."""
import sys, json, psycopg
sys.path.insert(0, "$REPO/src")
from atus_pipeline.api.dependencies import read_data_version
import importlib.util
spec = importlib.util.spec_from_file_location("b", __file__.replace("final_state.py", "build_serving_proto.py"))
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
PROTO, FULL = "postgresql://atus:atus@localhost:5441/atus", "postgresql://atus:atus@localhost:5434/atus"
with psycopg.connect(PROTO) as c:
    print("prototype validation_results rows before truncate:", c.execute("select count(*) from atus.validation_results").fetchone()[0])
    c.execute("TRUNCATE atus.validation_results RESTART IDENTITY"); c.commit()
with psycopg.connect(PROTO, autocommit=True) as c:
    c.execute("VACUUM (ANALYZE)")
with psycopg.connect(PROTO) as c:
    h = b.content_hash(c, "target")
    print("prototype content hash now:", h["content_hash"])
    print("prototype data_version (read_data_version):", read_data_version(c))
with psycopg.connect(FULL, options="-c default_transaction_read_only=on") as c:
    print("full DB data_version (read_data_version):", read_data_version(c))
