import sys
from pathlib import Path
import uvicorn
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings
s = Settings(database_url="postgresql://nobody@127.0.0.1:1/none", data_dir=Path("/tmp/none"), release="0325", log_level="WARNING")
uvicorn.run(create_app(s, open_pool=False), uds=sys.argv[1], log_level="warning")
