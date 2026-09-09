"""Environment-based configuration.

All settings come from environment variables (optionally via a `.env` file in
the working directory) so nothing machine-specific is hard-coded. Defaults are
safe for local development against the docker-compose PostgreSQL instance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_DATABASE_URL = "postgresql://atus:atus@localhost:5434/atus"
DEFAULT_DATA_DIR = "data"
DEFAULT_RELEASE = "0325"


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    release: str
    log_level: str

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging" / self.release

    @property
    def reference_dir(self) -> Path:
        return self.data_dir / "reference"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"


def load_settings() -> Settings:
    """Build settings from the environment, loading `.env` if present."""
    load_dotenv()
    data_dir = Path(os.environ.get("ATUS_DATA_DIR", DEFAULT_DATA_DIR))
    return Settings(
        database_url=os.environ.get("ATUS_DATABASE_URL", DEFAULT_DATABASE_URL),
        data_dir=data_dir,
        release=os.environ.get("ATUS_RELEASE", DEFAULT_RELEASE),
        log_level=os.environ.get("ATUS_LOG_LEVEL", "INFO").upper(),
    )
