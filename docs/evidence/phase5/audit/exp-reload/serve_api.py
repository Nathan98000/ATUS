"""Serve the real API (create_app) on 127.0.0.1:8201 against atus_audit_reload.
Same construction as scripts/e2e_backend.py, minus the DB rebuild."""
import logging
import sys
from pathlib import Path

import uvicorn

from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings

OUT = Path(__file__).resolve().parent
db = sys.argv[1] if len(sys.argv) > 1 else "atus_audit_reload"
settings = Settings(
    database_url=f"postgresql://atus:atus@localhost:5434/{db}",
    data_dir=OUT / "tmp_v1" / "data",
    release="0325",
    log_level="INFO",
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s")
app = create_app(settings)


# Audit-only instrumentation (harness, not repo code): an ASYNC route reporting the
# anyio default thread limiter and psycopg_pool stats. Being async it also shows
# whether the event loop itself stays responsive.
@app.get("/__audit/threads")
async def _audit_threads():
    import anyio.to_thread
    lim = anyio.to_thread.current_default_thread_limiter()
    st = lim.statistics()
    pool = app.state.pool.get_stats()
    return {"borrowed": st.borrowed_tokens, "total": st.total_tokens,
            "tasks_waiting": st.tasks_waiting,
            "pool_size": pool.get("pool_size"), "pool_available": pool.get("pool_available"),
            "requests_waiting": pool.get("requests_waiting"),
            "requests_errors": pool.get("requests_errors"),
            "requests_num": pool.get("requests_num")}


@app.get("/__audit/cache/{key}")
async def _audit_cache(key: str):
    entry = app.state.cache._entries.get(key)  # read-only peek (no LRU reordering)
    if entry is None:
        return {"present": False}
    return {"present": True, "value": entry.get("estimate", {}).get("value"),
            "confidence_level": entry.get("spec", {}).get("confidence_level")}


uvicorn.run(app, host="127.0.0.1", port=8201, log_level="warning")
