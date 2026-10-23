"""GET/POST endpoint diff full vs serving through the real FastAPI app (TestClient).
usage: python api_get_diff.py <full_url> <serving_url>"""
import json, sys
from dataclasses import replace
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
from atus_pipeline.config import load_settings
full, serv = sys.argv[1], sys.argv[2]
reqs = [("GET", "/health", None), ("GET", "/ready", None), ("GET", "/api/v1/meta", None),
        ("GET", "/api/v1/activities", None), ("GET", "/api/v1/activities/presets", None),
        ("GET", "/api/v1/activities/0101", None), ("GET", "/api/v1/activities/999999", None),
        ("GET", "/api/v1/population/metadata", None),
        ("POST", "/api/v1/analysis/estimate", {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2025]}),
        ("POST", "/api/v1/analysis/estimate", {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2019, 2020]}),
        ("POST", "/api/v1/analysis/estimate", {"measure": "nope", "activity": {"preset": "sleep"}, "years": [2025]}),
        ("POST", "/api/v1/analysis/estimate", {"measure": "average_minutes_per_day", "activity": {"include": ["999999"]}, "years": [2025]}),
        ("POST", "/api/v1/analysis/trend", {"measure": "participation_rate", "activity": {"preset": "watching_tv"}, "years": [2018, 2019, 2020, 2021], "variance": "none"}),
        ("POST", "/api/v1/analysis/compare", {"measure": "participation_rate", "activity": {"preset": "watching_tv"}, "years": [2024],
                                              "group_a": {"sex": "male"}, "group_b": {"sex": "female"}, "variance": "none"}),
        ]
out = {}
for label, url in (("full", full), ("serving", serv)):
    app = create_app(replace(load_settings(), database_url=url))
    with TestClient(app) as c:
        res = []
        for m, path, body in reqs:
            r = c.request(m, path, json=body)
            try: j = r.json()
            except Exception: j = r.text
            res.append((r.status_code, j))
        out[label] = res
for (m, path, body), (sf, jf), (ss, js) in zip(reqs, out["full"], out["serving"]):
    same = (sf, jf) == (ss, js)
    note = ""
    if not same and isinstance(jf, dict) and isinstance(js, dict):
        note = " differing top-level keys: " + str([k for k in set(jf) | set(js) if jf.get(k) != js.get(k)])
        if "data" in jf: note += f" | full data={jf.get('data')} serving data={js.get('data')}"
    print(f"{m:4s} {path:32s} status {sf}/{ss} identical={same}{note}")
