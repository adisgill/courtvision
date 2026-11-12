"""DuckDB connection and shared SQL macros.

Storage model: Parquet files under ``data/parquet`` are the durable copy of
every source. The DuckDB file holds views over those files (schemas
``kaggle``, ``nba_api``) and a conformed staging layer (schema ``stg``).
Rebuilding the database from Parquet takes seconds.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from courtvision.config import get_paths

# NBA game ids are 10 characters: "00" + season-type digit + 2-digit season
# start year + 5-digit game number, e.g. 0022500001 = 2025-26 regular season.
# Kaggle stores them as integers (leading zeros lost), nba_api as strings.
MACROS = """
create or replace macro cv_game_id(gid) as lpad(cast(gid as varchar), 10, '0');
create or replace macro cv_season(gid) as
    (case when substr(cv_game_id(gid), 4, 2)::int >= 46 then 1900 else 2000 end)
    + substr(cv_game_id(gid), 4, 2)::int;
create or replace macro cv_season_type(gid) as
    case substr(cv_game_id(gid), 3, 1)
        when '1' then 'preseason'
        when '2' then 'regular'
        when '3' then 'all_star'
        when '4' then 'playoffs'
        when '5' then 'play_in'
        when '6' then 'cup_final'
        else 'unknown'
    end;
create or replace macro cv_season_label(season) as
    cast(season as varchar) || '-' || lpad(cast((season + 1) % 100 as varchar), 2, '0');
create or replace macro cv_is_team_id(i) as try_cast(i as bigint) between 1610612737 and 1610612766;
-- Court zone from the recorded shot value and legacy coordinates (tenths of a foot, rim at
-- the origin). Reproduces the official SHOT_ZONE_BASIC on 99.99% of 2025-26 shots; see
-- docs/metrics_spec.md section 1. 'BACK' (backcourt heaves) is excluded from rated zones.
create or replace macro cv_zone(v, x, y) as
    case
        when x is null or y is null then null
        when v = 3 and y > 417.5 then 'BACK'
        when v = 3 and abs(x) >= 220 and y <= 87 then 'C3'
        when v = 3 then 'ATB3'
        when sqrt(x * x + y * y) < 40 then 'RA'
        when abs(x) <= 80 and y <= 138 then 'PAINT'
        else 'MID'
    end;
-- Seconds left in the period from an ISO-8601 clock such as 'PT11M56.00S'.
create or replace macro cv_clock_s(c) as
    regexp_extract(c, 'PT(\\d+)M', 1)::int * 60 + regexp_extract(c, 'M([\\d.]+)S', 1)::double;
-- Game seconds elapsed at the start of a period (12-minute quarters, 5-minute overtimes).
create or replace macro cv_period_start_s(p) as
    case when p <= 4 then (p - 1) * 720 else 2880 + (p - 5) * 300 end;
create or replace macro cv_period_len_s(p) as case when p <= 4 then 720 else 300 end;
"""


def connect(path: Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the project database and install the shared macros."""
    path = path or get_paths().duckdb
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(MACROS)
    return con


def sql_path(p: Path) -> str:
    """A filesystem path as a single-quoted SQL string literal."""
    return "'" + p.resolve().as_posix().replace("'", "''") + "'"


def sql_glob(directory: Path, pattern: str) -> str:
    """``directory/pattern`` as a SQL string literal (no resolve() on the wildcard)."""
    return "'" + (directory.resolve() / pattern).as_posix().replace("'", "''") + "'"
