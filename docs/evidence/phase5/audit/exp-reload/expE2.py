"""Exp E2: does the post-compute data_version re-check (analysis.py:87-94) keep
post-reload results out of pre-reload cache keys? Natural (non-emulated) race:
the loader commits data (T2) and then bumps the version in a separate
transaction (T3). N uncached requests block on the TRUNCATE locks, all wake at
T2 commit, compute, then re-read the version.
For each response we record X-Cache and X-Analysis-Key, recompute which
data_version that key corresponds to (cache_key() from the repo), and peek the
server's cache entry under that key."""
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

S = os.path.dirname(os.path.abspath(__file__))
PY = "$REPO/.venv/bin/python"
OUT = os.path.join(S, "expE2"); os.makedirs(OUT, exist_ok=True)
N, HOLD, ROUNDS = 10, 8, int(sys.argv[1]) if len(sys.argv) > 1 else 4
results = []
ctl = psycopg.connect("postgresql://atus:atus@localhost:5434/atus_audit_reload?application_name=expE2_ctl", autocommit=True)


def fire(i, rnd):
    body = {"activity": {"include": ["010101"]}, "years": [2023], "confidence_level": round(0.7 + rnd * 0.01 + i * 1e-4, 6)}
    t0 = time.time()
    r = httpx.post("http://127.0.0.1:8201/api/v1/analysis/estimate", json=body, timeout=120)
    j = r.json()
    return {"i": i, "status": r.status_code, "x_cache": r.headers.get("x-cache"), "key": r.headers.get("x-analysis-key"),
            "value": j.get("estimate", {}).get("value"), "spec": j.get("spec"), "dur": round(time.time() - t0, 3)}


for rnd in range(ROUNDS):
    old = ctl.execute("SELECT id FROM atus.ingestion_runs WHERE status='succeeded' ORDER BY id DESC LIMIT 1").fetchone()[0]
    ver = "v1" if rnd % 2 == 0 else "v2"
    loader = subprocess.Popen([PY, "run_loader.py", "atus_audit_reload", os.path.join(S, f"tmp_{ver}", "data"), str(HOLD),
                               os.path.join(OUT, f"events_r{rnd}.jsonl")], cwd=S,
                              stdout=open(os.path.join(OUT, f"loader_r{rnd}.out"), "w"), stderr=subprocess.STDOUT)
    time.sleep(1.5)
    with cf.ThreadPoolExecutor(N) as ex:
        resp = list(ex.map(lambda i: fire(i, rnd), range(N)))
    loader.wait()
    new = ctl.execute("SELECT id FROM atus.ingestion_runs WHERE status='succeeded' ORDER BY id DESC LIMIT 1").fetchone()[0]
    for r in resp:
        k_old = cache_key("estimate", r["spec"], ANALYTICS_VERSION, f"0325:run{old}")
        k_new = cache_key("estimate", r["spec"], ANALYTICS_VERSION, f"0325:run{new}")
        r["key_version"] = "old" if r["key"] == k_old else "new" if r["key"] == k_new else "?"
        peek = httpx.get(f"http://127.0.0.1:8201/__audit/cache/{r['key']}").json()
        r["cached_under_key"] = peek
        r.update(round=rnd, loaded=ver, old_run=old, new_run=new, loader_rc=loader.returncode)
        r.pop("spec")
        results.append(r)
        print(json.dumps(r), flush=True)
    time.sleep(1)

json.dump(results, open(os.path.join(OUT, "results.json"), "w"), indent=1)
lost = [r for r in results if r["x_cache"] == "miss" and r["key_version"] == "old"]
print(f"\nSUMMARY: {len(results)} blocked uncached requests over {ROUNDS} reloads; "
      f"x_cache counts={ {c: sum(1 for r in results if r['x_cache'] == c) for c in ('miss', 'bypass', 'hit')} }; "
      f"post-reload result cached under PRE-reload key: {len(lost)} "
      f"(values {sorted(set(r['cached_under_key'].get('value') for r in lost))})")
