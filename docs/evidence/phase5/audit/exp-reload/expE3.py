"""Exp E3: like E2 but the clients give up after 2 s (as in Exp E), and instead of
reading responses we peek the server cache under the PRE-reload and POST-reload
keys computed with the repo's own cache_key() + request->spec canonicalization."""
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time

import httpx
import psycopg

sys.path.insert(0, "$REPO/src")
from atus_pipeline.analytics import ANALYTICS_VERSION  # noqa: E402
from atus_pipeline.api.cache import cache_key  # noqa: E402
from atus_pipeline.api.schemas.analysis import EstimateRequest  # noqa: E402

S = os.path.dirname(os.path.abspath(__file__))
PY = "$REPO/.venv/bin/python"
OUT = os.path.join(S, "expE3"); os.makedirs(OUT, exist_ok=True)
N, HOLD, ROUNDS, CLIENT_TIMEOUT = 12, 8, int(sys.argv[1]), float(sys.argv[2])
ctl = psycopg.connect("postgresql://atus:atus@localhost:5434/atus_audit_reload?application_name=expE3_ctl", autocommit=True)
allrows = []


def fire(body):
    try:
        r = httpx.post("http://127.0.0.1:8201/api/v1/analysis/estimate", json=body, timeout=CLIENT_TIMEOUT)
        return r.status_code
    except httpx.TimeoutException:
        return "client_gave_up"


for rnd in range(ROUNDS):
    old = ctl.execute("SELECT id FROM atus.ingestion_runs WHERE status='succeeded' ORDER BY id DESC LIMIT 1").fetchone()[0]
    ver = "v1" if rnd % 2 == 0 else "v2"
    expected_new = 75.0 if ver == "v1" else 90.0
    loader = subprocess.Popen([PY, "run_loader.py", "atus_audit_reload", os.path.join(S, f"tmp_{ver}", "data"), str(HOLD),
                               os.path.join(OUT, f"events_r{rnd}.jsonl")], cwd=S,
                              stdout=open(os.path.join(OUT, f"loader_r{rnd}.out"), "w"), stderr=subprocess.STDOUT)
    time.sleep(1.5)
    bodies = [{"activity": {"include": ["010101"]}, "years": [2023],
               "confidence_level": round(0.6 + rnd * 0.01 + i * 1e-4, 6)} for i in range(N)]
    with cf.ThreadPoolExecutor(N) as ex:
        outcomes = list(ex.map(fire, bodies))
    loader.wait()
    time.sleep(0.5)
    new = ctl.execute("SELECT id FROM atus.ingestion_runs WHERE status='succeeded' ORDER BY id DESC LIMIT 1").fetchone()[0]
    for b, oc in zip(bodies, outcomes):
        spec = EstimateRequest(**b).to_spec().to_dict()
        k_old = cache_key("estimate", spec, ANALYTICS_VERSION, f"0325:run{old}")
        k_new = cache_key("estimate", spec, ANALYTICS_VERSION, f"0325:run{new}")
        p_old = httpx.get(f"http://127.0.0.1:8201/__audit/cache/{k_old}").json()
        p_new = httpx.get(f"http://127.0.0.1:8201/__audit/cache/{k_new}").json()
        row = {"round": rnd, "client": oc, "old_run": old, "new_run": new, "loaded": ver,
               "expected_new_value": expected_new, "under_old_key": p_old, "under_new_key": p_new,
               "loader_rc": loader.returncode}
        allrows.append(row)
        print(json.dumps(row), flush=True)
    time.sleep(1)

json.dump(allrows, open(os.path.join(OUT, "results.json"), "w"), indent=1)
bad = [r for r in allrows if r["under_old_key"].get("present")]
print(f"\nSUMMARY: {len(allrows)} requests over {ROUNDS} reloads (client timeout {CLIENT_TIMEOUT}s); "
      f"entries present under PRE-reload key: {len(bad)}; their values: "
      f"{[(r['round'], r['under_old_key'].get('value'), r['expected_new_value']) for r in bad]}")
