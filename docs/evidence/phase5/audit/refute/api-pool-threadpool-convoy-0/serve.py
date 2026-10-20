"""Unpatched real app under uvicorn on a UDS against scratch fixture DB."""
import sys
from pathlib import Path
import uvicorn
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings
settings = Settings(database_url="postgresql://atus:atus@localhost:5434/atus_audit_refconvoy",
                    data_dir=Path("/tmp/none"), release="0325", log_level="WARNING")
uvicorn.run(create_app(settings), uds=sys.argv[1], log_level="warning")
