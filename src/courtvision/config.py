"""Paths and environment. Everything on disk hangs off one data directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

KAGGLE_DATASET = "eoinamoore/historical-nba-data-and-player-box-scores"


@dataclass(frozen=True)
class Paths:
    """Layout of the (gitignored) data directory."""

    data: Path

    @property
    def kaggle_raw(self) -> Path:
        """Files exactly as downloaded from Kaggle. Never edited."""
        return self.data / "raw" / "kaggle"

    @property
    def nba_api_raw(self) -> Path:
        """One JSON file per nba_api response, keyed by endpoint + params."""
        return self.data / "raw" / "nba_api"

    @property
    def parquet(self) -> Path:
        """Typed Parquet copies of the raw sources; what DuckDB reads."""
        return self.data / "parquet"

    @property
    def duckdb(self) -> Path:
        return self.data / "courtvision.duckdb"


def load_env() -> None:
    """Load ``.env`` from the repo root without overriding real env vars."""
    load_dotenv(REPO_ROOT / ".env", override=False)


def get_paths() -> Paths:
    load_env()
    raw = os.environ.get("COURTVISION_DATA_DIR")
    data = Path(raw).expanduser() if raw else REPO_ROOT / "data"
    if not data.is_absolute():
        data = REPO_ROOT / data
    return Paths(data=data)
