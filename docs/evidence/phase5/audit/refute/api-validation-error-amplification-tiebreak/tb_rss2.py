"""Server-path memory only: body prebuilt before baseline; response not decoded."""
import json, resource, sys
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
def mb(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
n = int(sys.argv[1])
d = {"measure": "average_minutes_per_day", "activity": {"include": ["0101"]}, "years": [2023]}
for i in range(n): d[format(i, "x")] = 0
body = json.dumps(d, separators=(",", ":")).encode(); del d
with TestClient(create_app()) as c:
    c.get("/health")
    b = mb()
    r = c.post("/api/v1/analysis/estimate", content=body, headers={"Content-Type": "application/json"})
    print(f"n={n} req={len(body)} status={r.status_code} resp={len(r.content)} maxrss_before_MB={b:.1f} maxrss_after_MB={mb():.1f} delta_MB={mb()-b:.1f}")
