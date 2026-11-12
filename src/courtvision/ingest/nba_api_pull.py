"""Pull one season from nba_api into the raw cache, then into Parquet + DuckDB.

Pull order (each step resumes from the cache if interrupted):

1. ``leaguegamelog`` per season type -> nba_api's own list of games/teams
2. ``shotchartdetail`` per team per season type. The league-wide query
   (TeamID=0) silently truncates at 102,400 rows (~47% of a season), so
   it is never used.
3. ``playbyplayv3`` and ``gamerotation`` per game

Load parses every cached envelope into ``data/parquet/nba_api/*.parquet``
and creates ``nba_api.*`` views with keys conformed to the Kaggle tables
(10-char ``game_id``, BIGINT ``person_id`` / ``team_id``, ``season``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

import duckdb
import pandas as pd

from courtvision.db import sql_glob, sql_path
from courtvision.ingest.nba_api_client import CachedClient, read_envelope, result_sets

log = logging.getLogger(__name__)

SEASON_TYPES = {"regular": "Regular Season", "play_in": "PlayIn", "playoffs": "Playoffs"}


def season_string(start_year: int) -> str:
    """2025 -> '2025-26'."""
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def pull(
    client: CachedClient,
    season: int,
    season_types: Iterable[str] = SEASON_TYPES,
    *,
    progress_every: int = 50,
) -> dict[str, int]:
    from nba_api.stats.endpoints import (
        gamerotation,
        leaguegamelog,
        playbyplayv3,
        shotchartdetail,
    )

    s = season_string(season)
    games: dict[str, set[int]] = {}
    for st in season_types:
        r = client.get(
            leaguegamelog.LeagueGameLog,
            season=s,
            season_type_all_star=SEASON_TYPES[st],
            player_or_team_abbreviation="T",
        )
        for row in result_sets(r)["LeagueGameLog"]:
            games.setdefault(row["GAME_ID"], set()).add(row["TEAM_ID"])
    teams = sorted({t for ts in games.values() for t in ts})
    log.info("%s: %d games, %d teams", s, len(games), len(teams))

    for st in season_types:
        for team_id in teams:
            client.get(
                shotchartdetail.ShotChartDetail,
                team_id=team_id,
                player_id=0,
                context_measure_simple="FGA",
                season_nullable=s,
                season_type_all_star=SEASON_TYPES[st],
            )
    log.info("shots done (%d network calls so far)", client.network_calls)

    for i, game_id in enumerate(sorted(games), 1):
        client.get(playbyplayv3.PlayByPlayV3, game_id=game_id)
        client.get(gamerotation.GameRotation, game_id=game_id)
        if i % progress_every == 0 or i == len(games):
            log.info(
                "games %d/%d (network %d, cache hits %d)",
                i, len(games), client.network_calls, client.cache_hits,
            )  # fmt: skip
    return {"games": len(games), "teams": len(teams), "network_calls": client.network_calls}


# --------------------------------------------------------------------------- load


def _envelopes(cache_dir: Path, endpoint: str, season: int) -> Iterable[dict]:
    s = season_string(season)
    for path in sorted((cache_dir / endpoint).glob("*.json.gz")):
        env = read_envelope(path)
        p = env["params"]
        if p.get("Season") == s or str(p.get("GameID", ""))[3:5] == f"{season % 100:02d}":
            yield env


def _frames(cache_dir: Path, season: int) -> dict[str, pd.DataFrame]:
    out: dict[str, list[pd.DataFrame]] = {
        "game_log": [], "shots": [], "shot_zone_league_avg": [], "pbp": [], "rotations": [],
    }  # fmt: skip
    for env in _envelopes(cache_dir, "leaguegamelog", season):
        df = pd.DataFrame(result_sets(env["response"])["LeagueGameLog"])
        df["SEASON_TYPE_PARAM"] = env["params"]["SeasonType"]
        out["game_log"].append(df)
    for env in _envelopes(cache_dir, "shotchartdetail", season):
        sets = result_sets(env["response"])
        out["shots"].append(pd.DataFrame(sets["Shot_Chart_Detail"]))
        avg = pd.DataFrame(sets["LeagueAverages"])
        avg["SEASON_TYPE_PARAM"] = env["params"]["SeasonType"]
        out["shot_zone_league_avg"].append(avg)
    for env in _envelopes(cache_dir, "playbyplayv3", season):
        game = env["response"]["game"]
        df = pd.DataFrame(game["actions"])
        df["gameId"] = game["gameId"]
        out["pbp"].append(df)
    for env in _envelopes(cache_dir, "gamerotation", season):
        sets = result_sets(env["response"])
        for side in ("HomeTeam", "AwayTeam"):
            df = pd.DataFrame(sets[side])
            df["SIDE"] = side
            out["rotations"].append(df)
    frames = {k: pd.concat(v, ignore_index=True) for k, v in out.items() if v}
    if "shot_zone_league_avg" in frames:  # identical copy on every per-team response
        frames["shot_zone_league_avg"] = frames["shot_zone_league_avg"].drop_duplicates()
    return frames


VIEWS_SQL = """
create schema if not exists nba_api;

create or replace view nba_api.game_log as
select cv_game_id(GAME_ID) as game_id, cv_season(GAME_ID) as season,
       cv_season_type(GAME_ID) as season_type, TEAM_ID::bigint as team_id, *
from read_parquet({game_log});

create or replace view nba_api.shots as
select cv_game_id(GAME_ID) as game_id, cv_season(GAME_ID) as season,
       cv_season_type(GAME_ID) as season_type, GAME_EVENT_ID::int as action_number,
       PLAYER_ID::bigint as person_id, TEAM_ID::bigint as team_id, *
from read_parquet({shots});

create or replace view nba_api.shot_zone_league_avg as
select * from read_parquet({shot_zone_league_avg});

create or replace view nba_api.pbp as
select cv_game_id(gameId) as game_id, cv_season(gameId) as season,
       cv_season_type(gameId) as season_type,
       nullif(personId, 0)::bigint as person_id, nullif(teamId, 0)::bigint as team_id, *
from read_parquet({pbp});

create or replace view nba_api.rotations as
select cv_game_id(GAME_ID) as game_id, cv_season(GAME_ID) as season,
       cv_season_type(GAME_ID) as season_type, PERSON_ID::bigint as person_id,
       TEAM_ID::bigint as team_id,
       IN_TIME_REAL / 10.0 as in_seconds, OUT_TIME_REAL / 10.0 as out_seconds, *
from read_parquet({rotations});
"""


def load(con: duckdb.DuckDBPyConnection, cache_dir: Path, parquet_dir: Path, season: int) -> dict:
    """Write one Parquet file per table for ``season`` and (re)create the views.

    Files are per season (``<table>_<season>.parquet``); views read all seasons.
    """
    parquet_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, df in _frames(cache_dir, season).items():
        dest = parquet_dir / f"{name}_{season}.parquet"
        con.register("_df", df)
        con.execute(f"copy _df to {sql_path(dest)} (format parquet, compression zstd)")
        con.unregister("_df")
        counts[name] = len(df)
    globs = {
        name: sql_glob(parquet_dir, f"{name}_*.parquet")
        for name in ("game_log", "shots", "shot_zone_league_avg", "pbp", "rotations")
    }
    con.execute(VIEWS_SQL.format(**globs))
    return counts
