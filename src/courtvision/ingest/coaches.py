"""Head coach of every team-game (spec section 15.1).

The Kaggle box scores carry no coach (``coachId`` is empty in every season), so coaches
come from Basketball-Reference's coach records in the Kaggle dataset
``deocheng/nba-data-1946-2026`` (``public.coach_stats``: coach, season, team, games, wins,
losses; ``public.coaches``: names). A team-season with several coaches is split on the
game where each coach's win-loss record matches the team's results in order, so a
mid-season change lands on the right game. The current season is not in the records
yet: each team keeps the coach who finished last season, flagged ``assumed``.
"""

from __future__ import annotations

from itertools import permutations
from pathlib import Path

import duckdb
import pandas as pd

from courtvision.ingest.contracts import BBREF_TEAMS, _team_ids, fix_mojibake
from courtvision.ingest.salaries import norm_team

DATASET = "deocheng/nba-data-1946-2026"
FILES = ("csv/public.coaches.csv", "csv/public.coach_stats.csv")
# Basketball-Reference abbreviations of franchises that moved or were renamed (1996 on)
OLD_TEAMS = {"NJN": "BRK", "SEA": "OKC", "VAN": "MEM", "WSB": "WAS", "CHH": "CHO",
             "CHA": "CHO", "NOH": "NOP", "NOK": "NOP"}  # fmt: skip


def download(dest: Path) -> None:
    from courtvision.ingest.kaggle_download import _api

    dest.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        _api().dataset_download_file(DATASET, f, path=str(dest), quiet=True)


def split_games(wins: list[int], stints: list[tuple[str, int, int]]) -> list[str] | None:
    """Coach of each game, in order. ``wins``: 1/0 per game in date order; ``stints``:
    (coach, games, wins) in any order. Tries every order and keeps the one whose
    cumulative wins match; None if none does."""
    if sum(g for _, g, _ in stints) != len(wins):
        return None
    for order in permutations(stints):
        out, i, ok = [], 0, True
        for coach, g, w in order:
            if sum(wins[i : i + g]) != w:
                ok = False
                break
            out += [coach] * g
            i += g
        if ok:
            return out
    return None


def load(con: duckdb.DuckDBPyConnection, raw: Path) -> dict:
    """Write ``coach.coaches`` and ``coach.assignments`` (game_id, team_id, coach)."""
    names = pd.read_csv(next(raw.rglob("public.coaches.csv")), encoding="utf-8")
    names["coach_name"] = names.coach_name.map(fix_mojibake)
    stats = pd.read_csv(next(raw.rglob("public.coach_stats.csv")))
    stats = stats[(stats.league == "NBA") & (stats.reg_g > 0)].copy()
    stats["season"] = stats.season.str[:4].astype(int)
    stats = stats[stats.season >= 1996]
    ids = _team_ids(con)
    stats["abbr"] = stats.team.replace(OLD_TEAMS)
    stats["team_id"] = stats.abbr.map(lambda a: ids.get(norm_team(BBREF_TEAMS.get(a, ""))))
    games = con.execute(
        """select t.game_id, t.season, t.team_id, g.game_date, t.is_win::int as win
           from stg.team_games t join stg.games g using (game_id)
           where t.season_type = 'regular' and t.season >= 1996 and t.is_win is not null
           order by t.team_id, g.game_date, t.game_id"""
    ).df()
    last_season = int(stats.season.max())
    rows, unmatched = [], 0
    for (season, team), g in games.groupby(["season", "team_id"], sort=False):
        s = stats[(stats.season == season) & (stats.team_id == team)]
        assumed = False
        if s.empty and season > last_season:  # current season: last season's final coach
            prev = [r for r in rows if r["team_id"] == team and r["season"] == season - 1]
            if not prev:
                continue
            coaches, assumed = [prev[-1]["coach_id"]] * len(g), True
        else:
            coaches = split_games(g.win.tolist(), list(zip(s.coach_id, s.reg_g, s.reg_w,
                                                           strict=True)))  # fmt: skip
            if coaches is None:  # records don't add up: give every game the main coach
                unmatched += 1
                if s.empty:
                    continue
                coaches = [s.sort_values("reg_g").coach_id.iloc[-1]] * len(g)
        for gid, cid in zip(g.game_id, coaches, strict=True):
            rows.append({"game_id": gid, "season": season, "team_id": team, "coach_id": cid,
                         "assumed": assumed})  # fmt: skip
    a = pd.DataFrame(rows).merge(names[["coach_id", "coach_name"]], on="coach_id", how="left")
    con.execute("create schema if not exists coach")
    for name, df in (("coaches", names), ("assignments", a)):
        con.register("tmp_df", df)
        con.execute(f"create or replace table coach.{name} as select * from tmp_df")
        con.unregister("tmp_df")
    return {"games": len(a), "coaches": a.coach_id.nunique(), "team_seasons_unmatched": unmatched,
            "assumed_games": int(a.assumed.sum())}  # fmt: skip
