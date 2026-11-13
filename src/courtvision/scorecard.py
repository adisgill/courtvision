"""Prediction ledger and scorecard (spec section 21).

Every ``courtvision forecast`` run appends what it predicted to a ledger
(``forecast.ledger_games``, ``ledger_players``, ``ledger_availability``) with the time,
the last day of data it used, the injury report it used and the code version. Nothing
in the ledger is ever rewritten, so a prediction can be scored only as it was made.

``courtvision scorecard --date D`` scores the games of day ``D`` against what happened:
final scores and box scores from the NBA's live feed (``cdn.nba.com``, the source live
mode already uses; cached, at most one request per game every 10 s) or, once the nightly
Kaggle update has them, from the database. For each game the latest prediction made
before it ended is used, and ``after_tipoff`` says whether it was made after the game
had started (no in-game information is used either way: forecasts read only completed
games and the injury report).

* ``forecast.scorecard_games``    predicted vs final margin, winner, win chance (Brier)
* ``forecast.scorecard_players``  each predicted stat line vs the box score: error, inside
                                  the 80% range, and the season-average baseline's error
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime

import duckdb
import numpy as np
import pandas as pd

from courtvision.config import REPO_ROOT

log = logging.getLogger(__name__)

STATS = ["pts", "reb", "ast", "fg3m", "stocks", "tov"]
LIVE_STATS = {"pts": ["points"], "reb": ["reboundsTotal"], "ast": ["assists"],
              "fg3m": ["threePointersMade"], "stocks": ["steals", "blocks"],
              "tov": ["turnovers"]}  # fmt: skip


def _has(con, schema: str, table: str) -> bool:
    return bool(con.execute("select count(*) from information_schema.tables where "
                            "table_schema = ? and table_name = ?", [schema, table]
                            ).fetchall()[0][0])  # fmt: skip


def _commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=10)  # fmt: skip
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def log_predictions(con: duckdb.DuckDBPyConnection, predicted_at: datetime | None = None,
                    note: str = "") -> dict:  # fmt: skip
    """Append the current forecast tables to the ledger (never overwrites)."""
    when = pd.Timestamp(predicted_at or datetime.now()).floor("s")
    meta = {
        "predicted_at": when, "commit": _commit(), "note": note,
        "data_through": con.execute("select max(game_date) from stg.games where home_score "
                                    "is not null").fetchall()[0][0],
        "injury_report": con.execute("select max(report_time) from injury.reports"
                                     ).fetchall()[0][0] if _has(con, "injury", "reports")
        else None,
    }  # fmt: skip
    counts = {}
    for src, dst in (("games", "ledger_games"), ("player_lines", "ledger_players"),
                     ("availability", "ledger_availability")):  # fmt: skip
        if not _has(con, "forecast", src):
            continue
        df = con.execute(f"select * from forecast.{src}").df()
        for k, v in meta.items():
            df[k] = v
        con.register("tmp_df", df)
        if _has(con, "forecast", dst):
            con.execute(f"insert into forecast.{dst} by name select * from tmp_df")
        else:
            con.execute(f"create table forecast.{dst} as select * from tmp_df")
        con.unregister("tmp_df")
        counts[dst] = len(df)
    return {"predicted_at": str(when), **counts}


# ---- results -------------------------------------------------------------------------------


def _minutes(clock: str | None) -> float:
    m = re.match(r"PT(\d+)M([\d.]+)S", clock or "")
    return int(m.group(1)) + float(m.group(2)) / 60 if m else 0.0


def live_results(game_ids: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Final scores and player box scores from the live feed (only finished games)."""
    from courtvision import live

    games, players = [], []
    for gid in game_ids:
        box = live.fetch(gid, "boxscore").get("game", {})
        final = box.get("gameStatus") == 3
        h, a = box.get("homeTeam", {}), box.get("awayTeam", {})
        games.append({"game_id": gid, "final": final, "status": box.get("gameStatusText"),
                      "home_score": h.get("score"), "away_score": a.get("score"),
                      "tipoff": box.get("gameTimeUTC")})  # fmt: skip
        if not final:
            continue
        for team in (h, a):
            for p in team.get("players", []):
                s = p.get("statistics", {})
                row = {"game_id": gid, "team_id": team.get("teamId"),
                       "person_id": p.get("personId"),
                       "minutes": _minutes(s.get("minutesCalculated") or s.get("minutes")),
                       "played": p.get("played") == "1"}  # fmt: skip
                for k, cols in LIVE_STATS.items():
                    row[k] = sum(s.get(c, 0) or 0 for c in cols)
                players.append(row)
    return pd.DataFrame(games), pd.DataFrame(players)


def db_results(con, game_ids: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The same from the database, for games the nightly Kaggle update already has."""
    marks = ",".join("?" * len(game_ids))
    g = con.execute(f"""select game_id, true as final, 'Final' as status, home_score,
                               away_score, null as tipoff
                        from stg.games where game_id in ({marks}) and home_score is not null""",
                    game_ids).df()  # fmt: skip
    p = con.execute(f"""select game_id, team_id, person_id, minutes, minutes > 0 as played,
                               b_pts as pts, b_oreb + b_dreb as reb, b_ast as ast,
                               b_fg3m as fg3m, b_stl + b_blk as stocks, b_tov as tov
                        from metrics.player_game_counts where game_id in ({marks})""",
                    game_ids).df()  # fmt: skip
    return g, p


# ---- scoring -------------------------------------------------------------------------------


def score(con: duckdb.DuckDBPyConnection, day: str, source: str = "auto") -> dict:
    """Score day ``day``'s logged predictions. ``source``: 'db', 'live' or 'auto' (the
    database where it has the game, the live feed otherwise)."""
    lg = con.execute("select * from forecast.ledger_games where game_date::date = ?::date",
                     [day]).df()  # fmt: skip
    if lg.empty:
        raise ValueError(f"no logged predictions for {day}")
    ids = sorted(lg.game_id.unique())
    g_db, p_db = db_results(con, ids) if source in ("auto", "db") else (pd.DataFrame(),) * 2
    have = set(g_db.game_id) if len(g_db) else set()
    rest = [i for i in ids if i not in have]
    g_lv, p_lv = live_results(rest) if rest and source in ("auto", "live") else (
        pd.DataFrame(), pd.DataFrame())  # fmt: skip
    games = pd.concat([x for x in (g_db.assign(source="database") if len(g_db) else None,
                                   g_lv.assign(source="live feed") if len(g_lv) else None)
                       if x is not None], ignore_index=True)  # fmt: skip
    box = pd.concat([x for x in (p_db, p_lv) if len(x)], ignore_index=True) if (
        len(p_db) or len(p_lv)) else pd.DataFrame()  # fmt: skip
    games = games[games.final.astype(bool)]
    if games.empty:
        return {"day": day, "final_games": 0}

    # the latest prediction made for each game (the ledger keeps every run)
    lg["predicted_at"] = pd.to_datetime(lg.predicted_at)
    pick = lg.sort_values("predicted_at").groupby("game_id").tail(1)
    sc = pick.merge(games[["game_id", "home_score", "away_score", "source"]], on="game_id")
    sc["actual_margin"] = sc.home_score.astype(float) - sc.away_score.astype(float)
    sc["error"] = sc.pred_margin - sc.actual_margin
    sc["winner_right"] = np.sign(sc.pred_margin) == np.sign(sc.actual_margin)
    home_won = (sc.actual_margin > 0).astype(float)
    sc["p_winner"] = np.where(home_won == 1, sc.home_win_prob, 1 - sc.home_win_prob)
    sc["brier"] = (sc.home_win_prob - home_won) ** 2
    # scheduled tip-off (Eastern, from the schedule) vs when the prediction was made (the
    # machine's local time, Eastern here)
    sc["after_tipoff"] = sc.predicted_at > pd.to_datetime(sc.tipoff)
    sigma = float(con.execute("select any_value(sigma) from forecast.margin_model where "
                              "model <> 'player intervals'").fetchall()[0][0] or 13.9)  # fmt: skip
    sc["margin_inside_80"] = sc.error.abs() <= 1.2816 * sigma

    # players: the same run's lines for the scored games
    lp = con.execute("select * from forecast.ledger_players").df()
    lp["predicted_at"] = pd.to_datetime(lp.predicted_at)
    lp = lp.merge(pick[["game_id", "predicted_at"]], on=["game_id", "predicted_at"])
    lp = lp[lp.game_id.isin(sc.game_id)]
    pl = pd.DataFrame()
    if len(lp) and len(box):
        box = box.copy()
        box["person_id"] = box.person_id.astype("int64")
        lp["person_id"] = lp.person_id.astype("int64")
        m = lp.merge(box, on=["game_id", "person_id"], how="left", suffixes=("", "_act"))
        m["played_act"] = m["played"].fillna(False).astype(bool) & (m.minutes.fillna(0) > 0)
        # baseline: his per-game average this season before the game (what a box score says)
        base = con.execute(
            """select c.person_id, avg(c.b_pts) pts, avg(c.b_oreb + c.b_dreb) reb,
                      avg(c.b_ast) ast, avg(c.b_fg3m) fg3m, avg(c.b_stl + c.b_blk) stocks,
                      avg(c.b_tov) tov
               from metrics.player_game_counts c join stg.games g using (game_id)
               where c.season = (select max(season) from stg.games) and c.minutes > 0
                 and c.season_type = 'regular' and g.game_date < ?::date
               group by 1""", [day]).df().set_index("person_id")  # fmt: skip
        rows = []
        for r in m.itertuples():
            for s in STATS:
                act = getattr(r, f"{s}_act") if hasattr(r, f"{s}_act") else np.nan
                rows.append({"game_id": r.game_id, "team_id": r.team_id,
                             "person_id": r.person_id, "stat": s, "pred": getattr(r, s),
                             "lo": getattr(r, f"{s}_lo"), "hi": getattr(r, f"{s}_hi"),
                             "actual": act, "played": r.played_act,
                             "minutes": r.minutes if r.played_act else 0.0,
                             "exp_minutes": r.exp_minutes,
                             "season_avg": base[s].get(r.person_id, np.nan)
                             if s in base else np.nan})  # fmt: skip
        pl = pd.DataFrame(rows)
        pl["error"] = pl.pred - pl.actual
        pl["inside"] = (pl.actual >= np.floor(pl.lo)) & (pl.actual <= np.ceil(pl.hi))
        pl["avg_error"] = pl.season_avg - pl.actual
    keep = ["game_id", "game_date", "tipoff", "home_team_id", "away_team_id", "predicted_at",
            "after_tipoff", "data_through", "injury_report", "commit", "pred_margin",
            "home_win_prob", "home_score", "away_score", "actual_margin", "error",
            "winner_right", "p_winner", "brier", "margin_inside_80", "source"]  # fmt: skip
    sc = sc[[c for c in keep if c in sc.columns]].assign(scored_at=pd.Timestamp.now().floor("s"))
    for name, df in (("scorecard_games", sc), ("scorecard_players", pl)):
        if df.empty:
            continue
        con.register("tmp_df", df.assign(day=pd.Timestamp(day)))
        if _has(con, "forecast", name):
            con.execute(f"delete from forecast.{name} where day = ?::date and game_id in "
                        "(select game_id from tmp_df)", [day])  # fmt: skip
            con.execute(f"insert into forecast.{name} by name select * from tmp_df")
        else:
            con.execute(f"create table forecast.{name} as select * from tmp_df")
        con.unregister("tmp_df")
    played = pl[pl.played] if len(pl) else pl
    return {
        "day": day, "final_games": len(sc), "winners": f"{int(sc.winner_right.sum())}/{len(sc)}",
        "margin_mae": float(sc.error.abs().mean()), "brier": float(sc.brier.mean()),
        "players_scored": int(played.person_id.nunique()) if len(played) else 0,
        "inside_80": float(played.inside.mean()) if len(played) else np.nan,
        "model_mae_pts": float(played[played.stat == "pts"].error.abs().mean())
        if len(played) else np.nan,
        "season_avg_mae_pts": float(played[played.stat == "pts"].avg_error.abs().mean())
        if len(played) else np.nan,
    }  # fmt: skip
