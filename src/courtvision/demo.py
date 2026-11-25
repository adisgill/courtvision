"""A slim copy of the database for the hosted demo (``courtvision demo``).

The full database is several gigabytes, mostly every play of every game since 1996-97.
The demo keeps what the app shows: every table it reads, with play-level and game-level
rows only for the recent seasons (``--since``, default last season and this one) and the
season-level history whole, so career and era charts still go back as far as the data.
Tables only the pipeline itself needs are left out. Views over Parquet files become
tables, so the demo file stands alone.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import subprocess
from pathlib import Path

import duckdb

from courtvision.config import REPO_ROOT
from courtvision.db import MACROS

log = logging.getLogger(__name__)

RELEASE_TAG = "demo-data"  # the release the app downloads from (app/data.py DEMO_URL)

# tables only the pipeline reads (intermediate steps, play-by-play, stints, possessions)
SKIP = {
    "metrics.values_long", "metrics.event_counts", "metrics.event_facts",
    "metrics.presence_rolling_long", "metrics.presence_rolling", "metrics.box_player_game",
    "metrics.box_team_game", "metrics.oncourt_player_game", "metrics.presence_player_game",
    "metrics.pbp_stint_team", "metrics.game_pbp_qa", "coach.possessions", "coach.timeouts",
    "lineups.team_stints", "plays.turnover_events", "forecast.wf_players",
    "ncaa.player_seasons", "draft.outcomes",
}  # fmt: skip
# views the app reads, materialized: recent seasons only, or (small ones) whole
VIEWS_RECENT = ["stg.shots"]
VIEWS_WHOLE = [
    "stg.games", "stg.players", "kaggle.games", "kaggle.players", "kaggle.team_statistics",
    "kaggle.team_histories", "kaggle.league_schedule_24_25", "kaggle.league_schedule_25_26",
    "kaggle.league_schedule_26_27", "nba_api.shot_zone_league_avg",
]  # fmt: skip
# season-level tables kept for every season (career and era charts)
WHOLE = {"metrics.team_season_counts", "metrics.player_season_counts", "metrics.player_season",
         *VIEWS_WHOLE}  # fmt: skip
BIG = 100_000  # rows; smaller tables are copied whole


def _cols(con, name: str) -> set[str]:
    return {r[0] for r in con.execute(f"describe src.{name}").fetchall()}


def _filter(name: str, cols: set[str], since: int) -> str:
    if name == "metrics.ratings_long":
        # season ratings for every season (only the tier the app shows), game ratings recent
        return (f"where (level like '%season' and (level, season, measure, tier) in "
                f"(select (level, season, measure, tier) from src.metrics.best_tier)) "
                f"or (level like '%game' and season >= {since})")  # fmt: skip
    if name == "metrics.team_season":  # the app's season list: seasons with game-level data
        return f"where season >= {since}"
    if "season" in cols:
        return f"where season >= {since}"
    if "game_id" in cols:
        return f"where cv_season(game_id) >= {since}"
    if "gameId" in cols:
        return f"where cv_season(gameId) >= {since}"
    return ""


def build(src: Path, dest: Path, since: int) -> dict:
    """Write the demo database to ``dest`` from the full database ``src``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for p in (dest, dest.with_name(dest.name + ".wal")):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(dest))
    con.execute(MACROS)
    con.execute(f"attach '{src.as_posix()}' as src (read_only)")
    tables = con.execute(
        "select schema_name || '.' || table_name, estimated_size from duckdb_tables() "
        "where database_name = 'src' order by 1").fetchall()  # fmt: skip
    views = {r[0] for r in con.execute(
        "select schema_name || '.' || view_name from duckdb_views() "
        "where database_name = 'src'").fetchall()}  # fmt: skip
    out = {}
    for name, rows in [*tables, *((v, None) for v in VIEWS_RECENT + VIEWS_WHOLE if v in views)]:
        if name in SKIP:
            continue
        schema = name.split(".")[0]
        con.execute(f"create schema if not exists {schema}")
        cols = _cols(con, name)
        big = rows is None or rows >= BIG or name == "metrics.team_season"
        where = _filter(name, cols, since) if big and name not in WHOLE else ""
        con.execute(f"create table {name} as select * from src.{name} {where}")
        out[name] = con.execute(f"select count(*) from {name}").fetchall()[0][0]
        log.info("%-40s %12s rows%s", name, f"{out[name]:,}", f"  ({where})" if where else "")
    con.execute("create schema demo")
    con.execute(
        """create table demo.info as select now()::timestamp as built_at, ?::int as since,
                  (select min(season) from metrics.ratings_long) as first_season,
                  (select max(game_date) from stg.games where home_score is not null)
                      as data_through""",
        [since],
    )
    con.execute("detach src")
    con.execute("checkpoint")
    con.close()
    return {"tables": len(out), "rows": sum(out.values()),
            "mb": round(dest.stat().st_size / 2**20, 1)}  # fmt: skip


def publish(dest: Path, tag: str = RELEASE_TAG) -> str:
    """Gzip the demo database and replace the asset on the repo's ``tag`` release (with the
    GitHub CLI, signed in, run from the repo); the hosted app picks it up within hours."""
    gz = dest.with_name(dest.name + ".gz")
    with open(dest, "rb") as f, gzip.open(gz, "wb", compresslevel=6) as z:
        shutil.copyfileobj(f, z, 2**20)
    gh = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    subprocess.run([gh, "release", "upload", tag, str(gz), "--clobber"], check=True,
                   cwd=REPO_ROOT, capture_output=True, text=True)  # fmt: skip
    log.info("published %s (%.0f MB) to release %s", gz.name, gz.stat().st_size / 2**20, tag)
    return str(gz)
