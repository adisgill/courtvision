"""Head coach of every team-game (spec section 15.1).

The Kaggle box scores carry no coach (``coachId`` is empty in every season), so coaches
come from Basketball-Reference's coach records in the Kaggle dataset
``deocheng/nba-data-1946-2026`` (``public.coach_stats``: coach, season, team, games, wins,
losses; ``public.coaches``: names). A team-season with several coaches is split on the
game where each coach's win-loss record matches the team's results in order, so a
mid-season change lands on the right game. The current season is not in the records
yet: it comes from Basketball-Reference's season coaches page when one has been fetched
(``courtvision ingest coaches --fetch-current``, see ``bbref_coaches``), split the same
way; without it each team keeps the coach who finished last season, flagged ``assumed``.
``source`` says which: ``records``, ``current page`` or ``assumed``.
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


def split_games(wins: list[int], stints: list[tuple[str, int, int]],
                partial: bool = False) -> list[str] | None:  # fmt: skip
    """Coach of each game, in order. ``wins``: 1/0 per game in date order; ``stints``:
    (coach, games, wins) in any order. Tries every order and keeps the one whose
    cumulative wins match; None if none does. ``partial``: the records may be newer than
    the games (a current season), so the stint running past the last game only has to
    fit what's there."""
    total = sum(g for _, g, _ in stints)
    if total != len(wins) and not (partial and total >= len(wins)):
        return None
    for order in permutations(stints):
        out, i, ok = [], 0, True
        for coach, g, w in order:
            if i >= len(wins):
                break
            seen = wins[i : i + g]
            if len(seen) == g:
                ok = sum(seen) == w
            else:  # cut short by the end of the data: wins and losses so far must fit
                ok = sum(seen) <= w and len(seen) - sum(seen) <= g - w
            if not ok:
                break
            out += [coach] * len(seen)
            i += len(seen)
        if ok and len(out) == len(wins):
            return out
    return None


def load(con: duckdb.DuckDBPyConnection, raw: Path, current: pd.DataFrame | None = None,
         current_season: int | None = None, as_of: str = "") -> dict:  # fmt: skip
    """Write ``coach.coaches``, ``coach.assignments`` (game_id, team_id, coach, source)
    and ``coach.current`` (each team's coach now). ``current``: this season's coaches
    from Basketball-Reference's season page (``bbref_coaches.parse``), when fetched."""
    names = pd.read_csv(next(raw.rglob("public.coaches.csv")), encoding="utf-8")
    names["coach_name"] = names.coach_name.map(fix_mojibake)
    stats = pd.read_csv(next(raw.rglob("public.coach_stats.csv")))
    stats = stats[(stats.league == "NBA") & (stats.reg_g > 0)].copy()
    stats["season"] = stats.season.str[:4].astype(int)
    stats = stats[stats.season >= 1996]
    ids = _team_ids(con)

    def team_id(abbr: str):
        return ids.get(norm_team(BBREF_TEAMS.get(OLD_TEAMS.get(abbr, abbr), "")))

    stats["team_id"] = stats.team.map(team_id)
    if current is not None and current_season is not None:
        current = current.assign(team_id=current.team_abbr.map(team_id), season=current_season)
        new = current[~current.coach_id.isin(names.coach_id)][["coach_id", "coach_name"]]
        names = pd.concat([names, new.drop_duplicates("coach_id")], ignore_index=True)
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
        source = "records"
        cur = (current[(current.season == season) & (current.team_id == team) & (current.g > 0)]
               if current is not None and s.empty else None)  # fmt: skip
        if cur is not None and not cur.empty:  # this season, from the season page
            source = "current page"
            stints = list(zip(cur.coach_id, cur.g, cur.wins, strict=True))
            coaches = split_games(g.win.tolist(), stints, partial=True)
            if coaches is None:  # can't place the change: the newest coach gets them all
                unmatched += 1
                coaches = [cur.coach_id.iloc[-1]] * len(g)
        elif s.empty and season > last_season:  # no page: last season's final coach
            prev = [r for r in rows if r["team_id"] == team and r["season"] == season - 1]
            if not prev:
                continue
            coaches, source = [prev[-1]["coach_id"]] * len(g), "assumed"
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
                         "assumed": source == "assumed", "source": source})  # fmt: skip
    a = pd.DataFrame(rows).merge(names[["coach_id", "coach_name"]], on="coach_id", how="left")
    # each team's coach now: the season page's last-listed coach (pages list a team's
    # coaches in order), else the coach of the team's latest game
    latest = a.sort_values("game_id").groupby("team_id").tail(1)
    now = pd.DataFrame({"team_id": latest.team_id, "coach_id": latest.coach_id,
                        "coach_name": latest.coach_name, "assumed": latest.assumed,
                        "source": latest.source, "as_of": ""})  # fmt: skip
    if current is not None and not current.empty:
        page = current.dropna(subset=["team_id"]).groupby("team_id").tail(1)
        page = pd.DataFrame({"team_id": page.team_id.astype("int64"), "coach_id": page.coach_id,
                             "coach_name": page.coach_name, "assumed": False,
                             "source": "current page", "as_of": as_of})  # fmt: skip
        now = pd.concat([page, now[~now.team_id.isin(page.team_id)]], ignore_index=True)
    con.execute("create schema if not exists coach")
    for name, df in (("coaches", names), ("assignments", a), ("current", now)):
        con.register("tmp_df", df)
        con.execute(f"create or replace table coach.{name} as select * from tmp_df")
        con.unregister("tmp_df")
    return {"games": len(a), "coaches": a.coach_id.nunique(), "team_seasons_unmatched": unmatched,
            "by_source": a.source.value_counts().to_dict()}  # fmt: skip
