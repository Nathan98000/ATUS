"""Tiebreak reproduction: in-process TestClient against create_app, real DB read-only
(only SELECTs from the data_version dependency / GET routes). No timing measured."""
import gzip, json, os, resource, sys
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app

mode = sys.argv[1] if len(sys.argv) > 1 else "all"

def maxrss_mb():
    # macOS: ru_maxrss in bytes
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6

def body_extrakeys(n):
    d = {"measure": "average_minutes_per_day", "activity": {"include": ["0101"]}, "years": [2023]}
    for i in range(n):
        d[format(i, "x")] = 0
    return json.dumps(d, separators=(",", ":")).encode()

app = create_app()
with TestClient(app) as c:
    H = {"Content-Type": "application/json", "Accept-Encoding": "gzip"}
    def post(label, body, path="/api/v1/analysis/estimate"):
        r = c.post(path, content=body, headers=H)
        raw = r.content
        print(f"{label}: status={r.status_code} req_bytes={len(body)} resp_bytes={len(raw)} "
              f"ratio={len(raw)/len(body):.2f} content-encoding={r.headers.get('content-encoding')} "
              f"gzip_would_be={len(gzip.compress(raw))} code={r.json()['error']['code'] if r.status_code>=400 else '-'}")
        return r
    if mode == "rss":
        c.get("/health")
        before = maxrss_mb()
        body = body_extrakeys(100_000)
        r = post("extrakeys100k(rss)", body)
        print(f"maxrss_before_MB={before:.1f} maxrss_after_MB={maxrss_mb():.1f} (body build + request + client-side decode in same process)")
        sys.exit(0)
    # sanity: a valid request shape first
    for n in (100_000, 10_000, 5_000, 600):
        post(f"extrakeys{n}", body_extrakeys(n))
    r = post("extrakeys100k_sample", body_extrakeys(100_000))
    print("sample error entries:", json.dumps(r.json()["error"]["details"]["errors"][:2]), "count=", len(r.json()["error"]["details"]["errors"]))
    big = {"measure": "average_minutes_per_day", "activity": {"preset": "A" * 1_000_000}, "years": [2023]}
    post("preset1M", json.dumps(big).encode())
    lk = {"measure": "average_minutes_per_day", "activity": {"include": ["0101"]}, "years": [2023], "K" * 100_000: 0}
    post("longkey100k", json.dumps(lk).encode())
    r = c.get("/api/v1/activities", headers={"Accept-Encoding": "gzip"})
    print(f"GET /activities status={r.status_code} resp_bytes={len(r.content)} content-encoding={r.headers.get('content-encoding')} cache-control={r.headers.get('cache-control')} gzip_would_be={len(gzip.compress(r.content))}")
