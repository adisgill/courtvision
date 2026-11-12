"""Convert the raw Kaggle files to Parquet and expose them in DuckDB.

Every file in ``data/raw/kaggle`` is loaded generically (the dataset's file
list changes over time, e.g. a new ``LeagueSchedule27_28.csv`` each season):

* CSV  -> ``data/parquet/kaggle/<table>.parquet`` (types inferred from a full
  scan, never a sample) -> view ``kaggle.<table>``
* Parquet -> view ``kaggle.<table>`` directly over the raw file (no copy)

Raw views are exactly the source columns. Cleaning and key conformance live
in ``courtvision.ingest.staging``.
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

from courtvision.db import sql_path

# Columns whose inferred type must not drift between nightly releases.
_ID_COLUMNS = {
    "gameId": "BIGINT",
    "personId": "BIGINT",
    "teamId": "BIGINT",
    "playerteamId": "BIGINT",
    "opponentteamId": "BIGINT",
    "opponentTeamId": "BIGINT",
    "hometeamId": "BIGINT",
    "awayteamId": "BIGINT",
    "homeTeamId": "BIGINT",
    "awayTeamId": "BIGINT",
}


def table_name(filename: str) -> str:
    """``PlayerStatisticsExtended.csv`` -> ``player_statistics_extended``;
    ``LeagueSchedule24_25.csv`` -> ``league_schedule_24_25``."""
    stem = Path(filename).stem
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", stem)
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", "_", s)
    return s.lower()


def _csv_columns(con: duckdb.DuckDBPyConnection, path: Path) -> list[str]:
    rows = con.execute(f"describe select * from read_csv({sql_path(path)}, sample_size=-1)")
    return [r[0] for r in rows.fetchall()]


def load(con: duckdb.DuckDBPyConnection, raw_dir: Path, parquet_dir: Path) -> list[str]:
    """Load every raw Kaggle file; return the created ``kaggle.*`` view names."""
    parquet_dir.mkdir(parents=True, exist_ok=True)
    con.execute("create schema if not exists kaggle")
    created = []
    for src in sorted(raw_dir.iterdir()):
        name = table_name(src.name)
        if src.suffix.lower() == ".csv":
            dest = parquet_dir / f"{name}.parquet"
            cols = _csv_columns(con, src)
            overrides = {c: t for c, t in _ID_COLUMNS.items() if c in cols}
            types = ""
            if overrides:
                types = ", types = {" + ", ".join(f"'{c}': '{t}'" for c, t in overrides.items())
                types += "}"
            con.execute(
                f"copy (select * from read_csv({sql_path(src)}, sample_size = -1{types})) "
                f"to {sql_path(dest)} (format parquet, compression zstd)"
            )
        elif src.suffix.lower() == ".parquet":
            dest = src
        else:
            continue
        con.execute(
            f"create or replace view kaggle.{name} as select * from read_parquet({sql_path(dest)})"
        )
        created.append(f"kaggle.{name}")
    return created
