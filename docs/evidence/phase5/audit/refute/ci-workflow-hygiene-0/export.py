import json, sys
from pathlib import Path
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings
s=Settings(database_url="postgresql://unused:unused@localhost:1/unused",data_dir=Path("data"),release="0325",log_level="WARNING")
app=create_app(s,open_pool=False)
Path(sys.argv[1]).write_text(json.dumps(app.openapi(), indent=1)+"\n",encoding="utf-8")
