"""Read-only queries over the built ``metrics`` schema for the visualizer."""

from __future__ import annotations

import gzip
import os
import shutil
import threading
import urllib.request
from pathlib import Path

import duckdb
import pandas as pd

from courtvision.config import get_paths

# the demo database (`courtvision demo`, gzipped) as published on the repo's releases
DEMO_URL = ("https://github.com/adisgill/courtvision/releases/download/demo-data/"
            "courtvision.duckdb.gz")  # fmt: skip
_DOWNLOAD = threading.Lock()  # sessions share one process: one download at a time

HEADLINE = ["shooting", "defense", "ball_control", "backcourt", "frontcourt"]
LABELS = {
    "shooting": "Shooting",
    "defense": "Defense",
    "ball_control": "Ball control",
    "backcourt": "Backcourt",
    "frontcourt": "Frontcourt",
    "presence": "Presence",
}
COMPONENT_LABELS = {
    "shooting_sq": "Shot quality",
    "shooting_sm": "Shot making",
    "shooting_fd": "Drawing fouls",
    "shooting_fm": "Free-throw making",
    "defense_dq": "Shot quality allowed",
    "defense_dm": "Shot making allowed",
    "defense_dt": "Forced turnovers",
    "defense_df": "Fouls (FT rate allowed)",
    "defense_dr": "Defensive rebounding",
    "defense_dqm": "FG points allowed",
    "ball_bt": "Turnover avoidance",
    "ball_bo": "Offensive rebounding",
    "backcourt_sh": "Guard shooting",
    "backcourt_ft": "Guard free throws",
    "backcourt_bt": "Guard ball security",
    "backcourt_dz": "Perimeter defense",
    "backcourt_dt": "Guard steals",
    "backcourt_df": "Guard fouls",
    "frontcourt_sh": "Big shooting",
    "frontcourt_ft": "Big free throws",
    "frontcourt_bt": "Big ball security",
    "frontcourt_dz": "Paint defense",
    "frontcourt_dt": "Big steals",
    "frontcourt_df": "Big fouls",
    "frontcourt_dr": "Defensive rebounding",
    "defense_stl_rate": "Steals / 100 def. poss.",
    "defense_blk_rate": "Blocks / 100 def. poss.",
    "defense_pf_rate": "Fouls / 100 def. poss. (neg.)",
    "ball_shot_end_rate": "Plays ending in a shot",
    "ball_ato": "A/TO vs league (log)",
    "ball_ast_rate": "Assisted FG share vs league",
}
ORDER = list(LABELS) + list(COMPONENT_LABELS)


def ordered(measures) -> list[str]:
    """Headline dimensions first, then components in a fixed order."""
    rank = {m: i for i, m in enumerate(ORDER)}
    return sorted(measures, key=lambda m: rank.get(m, len(rank)))


def db_path() -> Path:
    """The full database if it's been built here, else the demo copy (``courtvision demo``)
    from the release ``DEMO_URL`` points at: downloaded the first time, and again whenever
    a newer one has been published (the hosted app has no data of its own)."""
    full = get_paths().duckdb
    if full.exists():
        return full
    demo = get_paths().data / "demo" / "courtvision.duckdb"
    url = os.environ.get("COURTVISION_DEMO_URL", DEMO_URL)
    with _DOWNLOAD:
        mark = demo.with_name(demo.name + ".version")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"),
                                        timeout=15) as r:  # fmt: skip
                version = r.headers.get("ETag") or r.headers.get("Last-Modified") or ""
        except OSError:
            version = None  # offline: keep the copy we have
        current = mark.read_text() if mark.exists() else ""
        if demo.exists() and (version is None or version == current):
            return demo
        demo.parent.mkdir(parents=True, exist_ok=True)
        part = demo.with_name(demo.name + ".part")
        with urllib.request.urlopen(url, timeout=60) as r, gzip.open(r) as z, \
                open(part, "wb") as f:  # fmt: skip
            shutil.copyfileobj(z, f, 2**20)
        part.replace(demo)  # open connections keep reading the old file until they close
        mark.write_text(version or "")
    return demo


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path()), read_only=True)


def demo_info(con) -> dict | None:
    """When and from what the demo copy was built (None for the full database)."""
    if not has_schema(con, "demo"):
        return None
    return q(con, "select * from demo.info").iloc[0].to_dict()


def q(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> pd.DataFrame:
    return con.execute(sql, params or []).df()


def seasons(con) -> list[int]:
    return q(con, "select distinct season from metrics.team_season order by 1 desc").season.tolist()


def team_names(con) -> pd.DataFrame:
    """Team name per season (franchises move and rename)."""
    return q(
        con,
        """select cv_season(gameId) as season, teamId as team_id,
                  any_value(teamCity || ' ' || teamName) as team
           from kaggle.team_statistics group by all""",
    )


def player_names(con) -> pd.DataFrame:
    return q(
        con,
        "select person_id, trim(coalesce(first_name, '') || ' ' || last_name) as player "
        "from stg.players",
    )


def ratings(con, level: str, where: str, params: list) -> pd.DataFrame:
    """Long ratings (best tier per season) for one level."""
    return q(
        con,
        f"""select r.* from metrics.ratings_long r
            join metrics.best_tier b using (level, season, measure, tier)
            where r.level = '{level}' and {where}""",
        params,
    )


def impact(con, person_id: int) -> pd.DataFrame:
    """Per-possession impact by season (adjusted plus-minus), with role context."""
    return q(
        con,
        """select i.*, r.usage, r.ast_pct, r.minutes
           from metrics.player_impact i
           left join metrics.player_role r
             on r.person_id = i.person_id and r.season = i.season and r.team_id = 0
            and r.season_type = 'regular'
           where i.person_id = ? order by i.season""",
        [person_id],
    )


def team_map(con, season: int) -> pd.DataFrame:
    """Offense and defense per 100 possessions vs league (box possessions, every era),
    record, and net, per team for one regular season."""
    return q(
        con,
        """with t as (
               select team_id, sum(b_pts) pts, sum(o_b_pts) opp, sum(b_poss) poss,
                      sum(o_b_poss) o_poss, count(*) filter (where b_pts > o_b_pts) wins,
                      count(*) filter (where b_pts < o_b_pts) losses
               from metrics.team_game_counts
               where season = ? and season_type = 'regular' group by team_id
           ), lg as (select sum(pts) / sum(poss) as ppp from t)
           select t.team_id, t.wins, t.losses,
                  100 * (t.pts / t.poss - lg.ppp) as offense,
                  100 * (lg.ppp - t.opp / t.o_poss) as defense,
                  100 * (t.pts / t.poss - t.opp / t.o_poss) as net
           from t, lg order by net desc""",
        [season],
    )


def team_dims(con, season: int, adjusted: bool) -> pd.DataFrame:
    """Wide 0-100 ratings of the five team dimensions (best tier), raw or opponent-adjusted."""
    col = "a.rating_adj" if adjusted else "a.rating"
    long = q(
        con,
        f"""select a.team_id, a.measure, {col} as rating
            from metrics.team_season_adj a
            join metrics.best_tier b
              on b.level = 'team_season' and b.season = a.season
             and b.measure = a.measure and b.tier = a.tier
            where a.season = ? and a.measure in
                  ('shooting', 'defense', 'ball_control', 'backcourt', 'frontcourt')""",
        [season],
    )
    if long.empty:
        return long
    return long.pivot_table(index="team_id", columns="measure", values="rating").reset_index()


def impact_leaders(con, season: int) -> pd.DataFrame:
    return q(
        con,
        """select i.person_id, i.impact, i.impact_off, i.impact_def, i.impact_se, i.poss,
                  i.points_added, i.qualified, pos.w_bc, r.usage, r.minutes,
                  trim(coalesce(p.first_name, '') || ' ' || p.last_name) as player
           from metrics.player_impact i
           join stg.players p using (person_id)
           left join metrics.positions pos using (person_id)
           left join metrics.player_role r
             on r.person_id = i.person_id and r.season = i.season and r.team_id = 0
            and r.season_type = 'regular'
           where i.season = ?""",
        [season],
    )


def has_market(con) -> bool:
    return bool(
        q(
            con, "select count(*) n from information_schema.tables where table_schema = 'market'"
        ).n.iloc[0]
    )


def zone_profile(con, counts_table: str, prefix: str, where: str, params: list) -> pd.DataFrame:
    """Per-zone FGA share and FG% of a unit vs league (``prefix`` 'a_' own, 'o_' allowed)."""
    zones = {"RA": "ra", "PAINT": "pnt", "MID": "mid", "C3": "c3", "ATB3": "atb3"}
    parts = " union all ".join(
        f"select '{z}' as zone, sum({prefix}fga_{k}) as fga, sum({prefix}fgm_{k}) as fgm "
        f"from {counts_table} c where {where}"
        for z, k in zones.items()
    )
    df = q(con, parts, params * len(zones))
    if df.fga.sum() == 0:
        return df
    season = params[0]
    # one row per zone (1994-97 had 3s inside today's arc, so a zone can hold two values)
    lg = q(
        con,
        """select zone, sum(share) as share, sum(fgm) / sum(fga) as fg_pct,
                  sum(xpts * fga) / sum(fga) as xpts
           from metrics.zone_baselines where season = ? and zone <> 'BACK' group by zone""",
        [season],
    )
    df = df.merge(lg, on="zone", how="left")
    total = df.fga.sum()
    df["unit_share"] = df.fga / total
    df["unit_fg_pct"] = df.fgm / df.fga.where(df.fga > 0)
    df["share_diff"] = df.unit_share - df.share
    df["fg_pct_diff"] = df.unit_fg_pct - df.fg_pct
    value = df.zone.map({"RA": 2, "PAINT": 2, "MID": 2, "C3": 3, "ATB3": 3})
    lg_pps = (df.share * df.xpts).sum() / df.share.sum()
    # Points per 100 of the unit's shots vs league average, split the way Shooting is:
    # selection (more shots from zones worth more than an average shot) and making
    # (more made than league average from the same spots). Each sums over zones to
    # the unit's shot quality / shot making on the FG-only scale.
    df["selection_pp100"] = 100 * df.share_diff * (df.xpts - lg_pps)
    df["making_pp100"] = 100 * (value * df.fgm - df.xpts * df.fga) / total
    return df


def shots(con, seasons: list[int], season_type: str = "regular", team_id: int | None = None,
          against: int | None = None, person_id: int | None = None) -> pd.DataFrame:  # fmt: skip
    """Shot locations of one unit: a team's (``team_id``), its opponents' (``against``)
    or a player's (``person_id``), over ``seasons``."""
    where, params = [f"s.season in ({','.join('?' * len(seasons))})", "s.season_type = ?"], [
        *seasons, season_type]  # fmt: skip
    if team_id is not None:
        where.append("s.team_id = ?")
        params.append(team_id)
    if against is not None:
        where.append("s.team_id <> ? and (g.home_team_id = ? or g.away_team_id = ?)")
        params += [against] * 3
    if person_id is not None:
        where.append("s.person_id = ?")
        params.append(person_id)
    return q(con, f"""select s.season, s.x_legacy::double as x, s.y_legacy::double as y,
                             s.made::int as made, s.shot_value
                      from stg.shots s join stg.games g using (game_id)
                      where {' and '.join(where)}""", params)  # fmt: skip


def league_shots(con, season: int, season_type: str = "regular") -> pd.DataFrame:
    return q(con, """select x_legacy::double as x, y_legacy::double as y, made::int as made,
                            shot_value
                     from stg.shots where season = ? and season_type = ?""",
             [season, season_type])  # fmt: skip


def has_schema(con, schema: str) -> bool:
    return bool(q(con, "select count(*) n from information_schema.tables "
                  "where table_schema = ?", [schema]).n.iloc[0])  # fmt: skip
