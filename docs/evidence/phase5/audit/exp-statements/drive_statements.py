"""For each request type: marker -> request -> settle -> marker; then extract
every statement the API's backends sent between the markers from the
server-side log (log_statement=all)."""
import json, sys, time, urllib.request, urllib.error
import psycopg
sys.path.insert(0, "docs/evidence/phase5/audit/exp-statements")
from pglog import read_entries, is_statement, short

API = "http://127.0.0.1:8204"
MARK = psycopg.connect("postgresql://atus:atus@127.0.0.1:5442/atus?application_name=marker", autocommit=True)
OUT = sys.argv[1]

def http(method, path, body=None, raw=None):
    data = None
    headers = {}
    if raw is not None:
        data = raw.encode(); headers["Content-Type"] = "application/json"
    elif body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, hdrs, payload = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, payload = e.code, dict(e.headers), e.read()
    dt = (time.perf_counter() - t0) * 1000
    return status, hdrs, payload, dt

EST = {"activity": {"include": ["010101"], "label": "Sleeping"}, "years": [2023]}
TREND = {"activity": {"include": ["010101"]}, "years": [2019, 2020, 2023]}
CMP = {"activity": {"include": ["010101"]}, "years": [2023], "group_a": {"sex": "male"},
       "group_b": {"sex": "female"}, "label_a": "men", "label_b": "women"}
CASES = [
    ("GET /health", "GET", "/health", None, None),
    ("GET /ready", "GET", "/ready", None, None),
    ("GET /api/v1/meta", "GET", "/api/v1/meta", None, None),
    ("GET /api/v1/meta (repeat)", "GET", "/api/v1/meta", None, None),
    ("GET /api/v1/activities", "GET", "/api/v1/activities", None, None),
    ("GET /api/v1/activities?search=sleep&level=3", "GET", "/api/v1/activities?search=sleep&level=3", None, None),
    ("GET /api/v1/activities/010101 (6-digit)", "GET", "/api/v1/activities/010101", None, None),
    ("GET /api/v1/activities/01 (2-digit)", "GET", "/api/v1/activities/01", None, None),
    ("GET /api/v1/activities/presets", "GET", "/api/v1/activities/presets", None, None),
    ("GET /api/v1/population/metadata", "GET", "/api/v1/population/metadata", None, None),
    ("POST estimate (cache miss)", "POST", "/api/v1/analysis/estimate", EST, None),
    ("POST estimate (cache hit)", "POST", "/api/v1/analysis/estimate", EST, None),
    ("POST trend (cache miss)", "POST", "/api/v1/analysis/trend", TREND, None),
    ("POST trend (cache hit)", "POST", "/api/v1/analysis/trend", TREND, None),
    ("POST compare (cache miss)", "POST", "/api/v1/analysis/compare", CMP, None),
    ("POST compare (cache hit)", "POST", "/api/v1/analysis/compare", CMP, None),
    ("POST estimate variance=none, pandemic (cache miss)", "POST", "/api/v1/analysis/estimate",
     {"activity": {"include": ["010101"]}, "years": [2019, 2020], "weights": "pandemic", "variance": "none"}, None),
    ("POST estimate 422 invalid_spec (age_min>age_max)", "POST", "/api/v1/analysis/estimate",
     {"activity": {"preset": "sleep"}, "years": [2023], "population": {"age_min": 54, "age_max": 25}}, None),
    ("POST estimate 422 invalid_spec (unknown preset)", "POST", "/api/v1/analysis/estimate",
     {"activity": {"preset": "napping"}, "years": [2023]}, None),
    ("POST estimate 422 unknown_activity (999999)", "POST", "/api/v1/analysis/estimate",
     {"activity": {"include": ["999999"]}, "years": [2023]}, None),
    ("POST estimate 422 unsupported_analysis (2020 multiyear)", "POST", "/api/v1/analysis/estimate",
     {"activity": {"include": ["010101"]}, "years": [2020]}, None),
    ("POST estimate 422 validation_error (schema)", "POST", "/api/v1/analysis/estimate",
     {"activity": {"include": ["010101"]}, "years": "notalist"}, None),
    ("POST estimate 400 malformed_json", "POST", "/api/v1/analysis/estimate", None, '{"activity": '),
    ("GET 404 unknown route", "GET", "/api/v1/nope", None, None),
    ("GET 405 method_not_allowed (GET estimate)", "GET", "/api/v1/analysis/estimate", None, None),
    ("GET /api/v1/activities/999999 404 activity_not_found", "GET", "/api/v1/activities/999999", None, None),
    ("GET /api/v1/activities/abc 404 activity_not_found (non-digit)", "GET", "/api/v1/activities/abc", None, None),
    ("GET /api/v1/activities?level=9 422 validation_error (query)", "GET", "/api/v1/activities?level=9", None, None),
    ("GET /openapi.json", "GET", "/openapi.json", None, None),
    ("GET /docs", "GET", "/docs", None, None),
]
# repeat a cache-miss estimate type with different specs to see prepared-statement switch-over
for i, yrs in enumerate([[2019], [2019, 2023], [2023], [2019]]):
    CASES.append((f"POST estimate (cache miss, extra #{i+1}, years={yrs}, sex=female)" if i < 3 else "POST estimate (cache hit extra #1 repeat)",
                  "POST", "/api/v1/analysis/estimate",
                  {"activity": {"include": ["010101"]}, "years": yrs, "population": {"sex": "female"}} if i < 3 else
                  {"activity": {"include": ["010101"]}, "years": [2019], "population": {"sex": "female"}}, None))
for i in range(3):
    CASES.append((f"GET /api/v1/meta (repeat {i+2})", "GET", "/api/v1/meta", None, None))

results = []
time.sleep(1.0)
for n, (label, method, path, body, raw) in enumerate(CASES, 1):
    MARK.execute(f"SELECT 'marker-{n}-begin'")
    time.sleep(0.3)
    status, hdrs, payload, dt = http(method, path, body, raw)
    time.sleep(0.7)
    MARK.execute(f"SELECT 'marker-{n}-end'")
    try:
        pj = json.loads(payload)
        err = pj.get("error", {}).get("code") if isinstance(pj, dict) and isinstance(pj.get("error"), dict) else None
    except Exception:
        err = None
    results.append({"n": n, "label": label, "status": status, "error_code": err,
                    "x_cache": hdrs.get("x-cache") or hdrs.get("X-Cache"), "ms": round(dt, 1),
                    "request_id": hdrs.get("x-request-id") or hdrs.get("X-Request-ID")})
    print(n, label, status, err, results[-1]["x_cache"], f"{dt:.1f}ms", flush=True)
time.sleep(1.0)

entries = read_entries()
# attribute API-backend entries to markers
idx = {}
for i, e in enumerate(entries):
    if e["app"] == "marker" and "marker-" in e["msg"]:
        tag = e["msg"].split("'")[1]
        idx[tag] = i
for r in results:
    b, en = idx[f"marker-{r['n']}-begin"], idx[f"marker-{r['n']}-end"]
    seg = entries[b + 1:en]
    api = [e for e in seg if e["app"] == "atus-api"]
    other = [e for e in seg if e["app"] not in ("atus-api", "marker")]
    r["statements"] = [dict(zip(("protocol", "sql"), short(e)), pid=e["pid"]) for e in api if is_statement(e)]
    r["non_statement_log"] = [f"[{e['pid']}] {e['level']}: {e['msg'][:200]}" for e in api if not is_statement(e)]
    r["other_apps"] = [f"[{e['pid']}] {e['app']} {e['level']}: {e['msg'][:120]}" for e in other]
    r["count"] = len(r["statements"])
json.dump(results, open(OUT, "w"), indent=1)
print("written", OUT)
