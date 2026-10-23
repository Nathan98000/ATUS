"""Run the real app under real uvicorn on a Unix socket (no TCP port), against the
scratch fixture DB. Test-only patch: AnalysisEngine.estimate first runs
`SELECT pg_sleep(n)` on the request's own connection when the activity label is
'sleep=<n>;...', emulating a long analysis SQL. Nothing else is changed."""
import os, sys
from pathlib import Path
import uvicorn
REPO = Path("$REPO")
from atus_pipeline.analytics import engine as eng
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings

_orig = eng.AnalysisEngine.estimate
def _slow(self, spec):
    label = spec.activity.label or ""
    if label.startswith("sleep="):
        secs = float(label.split(";")[0].split("=")[1])
        if secs > 0:
            self._conn.execute("SELECT pg_sleep(%s)", (secs,))
    return _orig(self, spec)
eng.AnalysisEngine.estimate = _slow

settings = Settings(database_url="postgresql://atus:atus@localhost:5434/atus_refute_convoy",
                    data_dir=Path("/tmp/none"), release="0325", log_level="INFO")
uvicorn.run(create_app(settings), uds=sys.argv[1], log_level="info")
