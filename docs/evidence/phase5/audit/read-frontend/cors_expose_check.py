"""Does the real API (CORS configured) let a cross-origin browser read X-Request-ID / X-Analysis-Key / X-Cache?
No database: uses /health and a 404 (both pass through the request_context + CORS middleware)."""
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
import dataclasses
from atus_pipeline.config import load_settings

app = create_app(dataclasses.replace(load_settings(), database_url="postgresql://invalid:1/none", api_cors_origins=("https://frontend.example",)), open_pool=False)
with TestClient(app) as c:
    for path in ("/health", "/api/v1/nope"):
        r = c.get(path, headers={"Origin": "https://frontend.example"})
        print(path, r.status_code, {k: v for k, v in r.headers.items() if k.lower().startswith(("access-control", "x-"))})
    r = c.options("/api/v1/analysis/estimate", headers={"Origin": "https://frontend.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    print("preflight", r.status_code, {k: v for k, v in r.headers.items() if k.lower().startswith("access-control")})
