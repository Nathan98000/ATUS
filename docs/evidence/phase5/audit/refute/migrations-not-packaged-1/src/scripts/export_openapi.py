"""Export the API's OpenAPI schema to frontend/openapi.json (no database needed).

The frontend generates its TypeScript API types from this snapshot:

    python scripts/export_openapi.py
    cd frontend && npm run generate:api-types

Run both after any change to the API schemas, then commit the regenerated
files, so the frontend types cannot drift from the actual backend contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings


def main() -> None:
    settings = Settings(
        database_url="postgresql://unused:unused@localhost:1/unused",
        data_dir=Path("data"),
        release="0325",
        log_level="WARNING",
    )
    app = create_app(settings, open_pool=False)
    target = Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"
    target.write_text(json.dumps(app.openapi(), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
