import os, time, sys
os.environ.pop("ATUS_DATABASE_URL", None); os.environ.pop("ATUS_LOG_LEVEL", None)
from atus_pipeline.config import load_settings
s = load_settings()
print("cwd", os.getcwd(), "db", s.database_url, "log_level(from cwd .env would be DEBUGPROBE):", s.log_level)
import atus_pipeline.config as c; print("config file", c.__file__)
from dotenv import find_dotenv; print("find_dotenv from config dir would be", "repo .env exists?", os.path.exists("$REPO/.env"))
from dataclasses import replace
from atus_pipeline.api.app import create_app
from fastapi.testclient import TestClient
app = create_app(replace(s, database_url="postgresql://atus:atus@localhost:5999/atus", log_level="INFO"))
with TestClient(app) as cl:
    print("health", cl.get("/health").status_code)
    t=time.time(); r=cl.get("/ready"); print("ready", r.status_code, round(time.time()-t,2), "s", r.text[:200])
